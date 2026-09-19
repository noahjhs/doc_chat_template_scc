import re
from typing import Any, Literal

import re2
from pydantic import BaseModel, Field, field_validator, model_validator


class SignupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    username: str
    token: str


class VerifyResponse(BaseModel):
    valid: bool
    username: str | None = None


class RevokeResponse(BaseModel):
    revoked: bool


class HostPairRequest(BaseModel):
    routing_key: str
    hostname: str | None = None
    label: str | None = None


class HostPairResponse(BaseModel):
    host_id: int
    device_token: str
    command_key: str
    label: str


class HostVerifyResponse(BaseModel):
    valid: bool


class HostPresenceReport(BaseModel):
    local_agent_url: str
    # The daemon's own single confined directory (its homeRoot -- see
    # agent/internal/commands.Handler) -- reported so this service can
    # cache it and use it as the join base for a rule's best-effort "."
    # path_resolution preview (see policy.py's module docstring); the Go
    # daemon itself is what actually enforces this accurately.
    cwd: str = ""


# The "matches nothing" sentinel a blank blacklist defaults to -- a
# character class requiring one character that's simultaneously not
# whitespace (\s) and not non-whitespace (\S), which is every character,
# so it can never match, not even a zero-width match against "". Confirmed
# directly under google-re2. Keeps Pattern's two fields fully symmetric:
# both are always real, always-compiled regex strings, never None -- an
# absent whitelist ("") already matches everything on its own (an empty
# RE2 pattern matches everything), but an absent blacklist can't use the
# same trick, since a literal "" blacklist would match (and so reject)
# everything instead of nothing. Mirrored wherever this schema needs
# re-deriving client-side (currently the harness) -- a separate,
# independently-deployed service, so it can't just be imported from here.
BLACKLIST_MATCHES_NOTHING = r"[^\s\S]"


class Pattern(BaseModel):
    """A whitelist/blacklist pair evaluated as RE2 regex (google-re2, real
    Python bindings to the same RE2 library Go's stdlib regexp targets --
    chosen specifically for RE2's guaranteed-linear-time matching, since
    these patterns evaluate agent-influenced input) against one argument
    value. Matches if (the value matches whitelist) AND (the value does
    NOT match blacklist).

    An earlier version had a "{roots}" whitelist sentinel meaning "must
    resolve to a path inside this host's own addressable directories" --
    removed for simplicity (the daemon's own path confinement was always
    the real security boundary regardless, which is why a "{roots}" rule
    could never be tier "allow" -- see the removed validator's own
    history). A rule author wanting that protection today writes a plain
    regex instead (e.g. a blacklist on "^/" and "\\.\\." to reject
    absolute paths and traversal), the same way every other constraint in
    this schema already works -- optionally with path_resolution set so
    that regex is checked against a RESOLVED value (see the Go daemon's
    own resolveForMatch, agent/internal/commands/policy.go) rather than
    the raw argument string, closing a real blind spot plain regex alone
    can't see (a "safe-looking" relative argument that's secretly a
    symlink into somewhere unsafe). path_resolution is one of "" (no
    resolution, today's raw-string behavior), "." (relative to the call's
    own cwd), "$PATH", or "MANPATH" -- see PolicyLayerRuleCreateRequest's
    own docstring for why position 0 (the binary) and cwd itself never
    need this flag. This service can only best-effort APPROXIMATE "."
    resolution (string-join against a cached cwd -- see
    _connected_host_configs) and can't meaningfully resolve "$PATH"/
    "MANPATH" at all, since it has no access to the real target
    filesystem/environment -- the Go daemon alone can do this accurately,
    and is what actually enforces it; see policy.py's own module
    docstring for why that's still safe.

    Both fields are always real regex strings, never absent/null -- a
    blank whitelist defaults to "" (an empty RE2 pattern matches
    everything, so "not specified" and "matches anything" coincide for
    free) and a blank blacklist defaults to BLACKLIST_MATCHES_NOTHING
    (which can never match, so "not specified" and "never rejects"
    likewise coincide). This is the ONLY shape a constraint takes, for
    both a positional_constraints list entry and an OptionConstraint's
    pattern (see PolicyLayerRuleCreateRequest/OptionConstraint below) --
    no "*" or other sentinel needed, because a MISSING value (a
    positional argument beyond what was supplied, or an option present
    with no value) is matched as "" for this purpose. That gives exactly
    the three states "value required"/"not required"/"not allowed" for
    free:
      - fully blank (default whitelist/blacklist): "" already satisfies
        an empty whitelist and never satisfies BLACKLIST_MATCHES_NOTHING,
        so this matches whether a value was actually supplied or not --
        "value not required".
      - whitelist "^$" (matches only ""): satisfied only by a missing
        value (or an explicit empty string) -- "value not allowed".
      - any other real whitelist: "" won't usually satisfy it, so a value
        must actually have been supplied -- "value required"."""

    whitelist: str = Field(default="", max_length=500)
    blacklist: str = Field(default=BLACKLIST_MATCHES_NOTHING, max_length=500)
    path_resolution: Literal["", ".", "$PATH", "MANPATH"] = ""

    @model_validator(mode="after")
    def _validate(self):
        for value in (self.whitelist, self.blacklist):
            try:
                re2.compile(value)
            except re2.error as e:
                # re2's own error message comes through as bytes (its args[0]) --
                # decoded here so the surfaced message isn't literally "b'...'".
                detail = e.args[0].decode("utf-8", "replace") if e.args and isinstance(e.args[0], bytes) else str(e)
                raise ValueError(f"Invalid regex {value!r}: {detail}") from e
        return self


class OptionConstraint(BaseModel):
    """One option (called "options", not "flags" -- they can carry values)
    a rule constrains, identified by its short and/or long form (at least
    one required). Including an OptionConstraint at all means that option
    must be PRESENT; pattern (always a Pattern, see its own docstring) is
    checked against its value -- or against "" if it was present with no
    value -- so a blank pattern accepts any/no value, and a whitelist of
    "^$" requires no value specifically."""

    short: str | None = Field(default=None, max_length=16)
    long: str | None = Field(default=None, max_length=64)
    pattern: Pattern = Field(default_factory=Pattern)

    @model_validator(mode="after")
    def _short_or_long(self):
        if not self.short and not self.long:
            raise ValueError("At least one of short/long is required.")
        return self


class PolicyLayerRuleCreateRequest(BaseModel):
    """One rule within a Policy Layer -- see policy_layer_rules' own
    schema comment in db.py for the full shape. positional_constraints'
    list index IS the argv position (index 0 = the binary, always matched
    by the Go daemon against its own $PATH-resolved absolute path -- see
    agent/internal/commands/policy.go's Rule -- so a rule constraining
    position 0 should match a real absolute path, not a bare command
    name; this service's own approximate preview, see policy.py, can't
    replicate that resolution and matches position 0 raw). Position 0 is
    optional exactly like every other position -- an empty list, or a
    blank entry, both mean "value not required" there too; there's
    nothing special required about constraining the binary itself. cwd
    optionally constrains the directory the call runs in -- unlike a
    positional/option constraint, it's never resolution-flagged (see
    Pattern's own docstring): cwd isn't a command argument, it's something
    the daemon already knows and has already resolved to an absolute path
    by the time it's checked, so no path_resolution mechanism applies to
    it. A blank cwd (the Pattern default) means "any directory"."""

    positional_constraints: list[Pattern] = Field(default_factory=list)
    option_constraints: list[OptionConstraint] = Field(default_factory=list)
    cwd: Pattern = Field(default_factory=Pattern)
    tier: Literal["allow", "ask", "deny"] = "ask"


class PolicyLayerRuleUpdateRequest(BaseModel):
    """PATCH /policy-layers/{id}/rules/{rule_id}'s body -- every field
    optional, same merge-only-what's-present convention as
    ProfileUpdateRequest."""

    positional_constraints: list[Pattern] | None = None
    option_constraints: list[OptionConstraint] | None = None
    cwd: Pattern | None = None
    tier: Literal["allow", "ask", "deny"] | None = None


class PolicyLayerRuleInfo(BaseModel):
    id: int
    position: int
    positional_constraints: list[Pattern]
    option_constraints: list[OptionConstraint]
    cwd: Pattern = Field(default_factory=Pattern)
    tier: str


class PolicyLayerRuleReorderRequest(BaseModel):
    rule_ids: list[int] = Field(min_length=1)


class HostPolicyLayerInfo(BaseModel):
    """The subset of a policy layer relevant once it's already known to be
    attached to a specific host -- embedded in HostInfo below
    (browser-facing, via GET /hosts) and returned by the daemon-facing
    GET /hosts/policy-layers. No host_ids on either: both callers already
    know which host they're asking about."""

    id: int
    name: str
    rules: list[PolicyLayerRuleInfo] = []


class HostPolicyLayerListResponse(BaseModel):
    policy_layers: list[HostPolicyLayerInfo]


class PolicyLayerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class PolicyLayerRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class PolicyLayerInfo(BaseModel):
    id: int
    name: str
    rules: list[PolicyLayerRuleInfo] = []
    host_ids: list[int] = []


class PolicyLayerListResponse(BaseModel):
    policy_layers: list[PolicyLayerInfo]


class PolicyEvalOption(BaseModel):
    """One supplied option for POST /policies/eval -- the same {short, long,
    value} shape run_shell_command's own tool schema uses; value omitted
    (None) means a valueless option, matched as "" same as everywhere else
    in this schema."""

    short: str | None = None
    long: str | None = None
    value: str | None = None


class PolicyEvalRequest(BaseModel):
    """POST /policies/eval's body -- composes policy_layer_ids (sorted
    ascending, concatenated -- the same v1 composition rule the Go daemon's
    composePolicy and conversations.py's own tier decision use) into one
    Policy and evaluates one hypothetical call against it. No side effects,
    no daemon involved -- the fast/deterministic bottom of the testing
    pyramid."""

    policy_layer_ids: list[int] = Field(default_factory=list)
    positional_args: list[str] = Field(default_factory=list)
    options: list[PolicyEvalOption] = Field(default_factory=list)
    cwd: str = ""


class PolicyEvalResponse(BaseModel):
    tier: Literal["allow", "ask", "deny"]
    matched_layer_id: int | None = None
    matched_rule: PolicyLayerRuleInfo | None = None


class ConversationToolCall(BaseModel):
    """Injects a tool call directly, as if the model had already proposed
    it -- what the harness's `call tool`/`call mock tool` verbs use. See
    conversations.py's new_turn_from_tool_call for why this skips straight
    to dispatch instead of asking the model to decide."""

    name: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)


class ConversationStepRequest(BaseModel):
    """POST /conversations/step's body. Exactly one of three things is
    happening on a given call, distinguished by shape rather than a mode
    flag:
      - turn is None: start a brand-new conversation -- message or
        tool_call required.
      - turn is present and NOT in flight (its own pending_calls/
        awaiting_approval are both empty -- see conversations.is_in_flight):
        start a new turn continuing the SAME conversation -- message or
        tool_call required again; the returned turn's previous_response_id
        carries forward so the model keeps its context.
      - turn is present and IS in flight: resume a turn paused for
        approval -- approval_decision required, message/tool_call invalid
        (a turn already mid-hop can't have a new message injected into it).
    default_host names which of the caller's own connected hosts a tool
    call should default to when it doesn't specify one and more than one
    is connected (mirrors run_shell_command's own `host` field -- this is
    a label, never a URL/API key; the server looks those up itself from
    its own state). mock, see conversations.py's own module docstring."""

    turn: dict[str, Any] | None = None
    message: str | None = None
    tool_call: ConversationToolCall | None = None
    approval_decision: Literal["allow", "deny"] | None = None
    default_host: str | None = None
    mock: bool = False

    @model_validator(mode="after")
    def _validate(self):
        if self.message is not None and self.tool_call is not None:
            raise ValueError("Supply at most one of message/tool_call.")
        return self


class ConversationPendingApproval(BaseModel):
    call_id: str
    host: str | None = None
    args: dict[str, Any]
    approval_id: str | None = None


class ConversationStepResponse(BaseModel):
    turn: dict[str, Any]
    status: Literal["done", "pending_approval"]
    message: str | None = None
    pending_approval: ConversationPendingApproval | None = None


class HostInfo(BaseModel):
    host_id: int
    label: str
    hostname: str | None = None
    connected: bool
    local_agent_url: str | None = None
    cwd: str = ""
    command_key: str | None = None
    environment_ids: list[int] = []
    policy_layers: list[HostPolicyLayerInfo] = []


class HostListResponse(BaseModel):
    hosts: list[HostInfo]


class HostRenameRequest(BaseModel):
    label: str = Field(min_length=1, max_length=64)


class EnvironmentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class EnvironmentRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class EnvironmentInfo(BaseModel):
    id: int
    name: str
    host_ids: list[int] = []


class EnvironmentListResponse(BaseModel):
    environments: list[EnvironmentInfo]


class SignOutAllResponse(BaseModel):
    signed_out_hosts: int


class AttendedHostUpdateRequest(BaseModel):
    host_id: int


class AttendedHostInfo(BaseModel):
    host_id: int | None = None
    label: str | None = None


class PendingApprovalCreateRequest(BaseModel):
    """POST /hosts/pending-approvals's body -- mirrors exactly what a
    caller resolving the pending_approval from POST /conversations/step
    already has, so the two channels (a native dialog on the attended
    host vs. resolving approval_decision directly) show the human the
    same thing."""

    template_name: str = Field(min_length=1, max_length=64)
    binary: str = Field(min_length=1, max_length=200)
    args: str = Field(default="", max_length=2000)
    host_label: str = Field(min_length=1, max_length=64)


class PendingApprovalCreateResponse(BaseModel):
    approval_id: str


class PendingApprovalInfo(BaseModel):
    id: str
    template_name: str
    binary: str
    args: str
    host_label: str
    decision: Literal["allow", "deny"] | None = None
    created_at: str


class PendingApprovalListResponse(BaseModel):
    """GET /hosts/pending-approvals' response -- at most one entry in
    practice (a daemon only ever has one user attending it at a time), but
    a list keeps the shape open-ended rather than assuming that."""

    pending_approvals: list[PendingApprovalInfo]


class PendingApprovalDecisionRequest(BaseModel):
    decision: Literal["allow", "deny"]


class StorageFileInfo(BaseModel):
    filename: str
    size: int
    uploaded_at: str


class StorageListResponse(BaseModel):
    files: list[StorageFileInfo]
    total_bytes: int
    cap_bytes: int


class StorageUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content: str  # base64


class StorageDownloadResponse(BaseModel):
    filename: str
    content: str  # base64


class StorageDeleteResponse(BaseModel):
    deleted: bool


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email_value(value: str) -> str:
    if not _EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address.")
    return value


def _validate_sms_number_value(value: str) -> str:
    """Normalizes to a canonical "(XXX) XXX-XXXX" US phone number -- the
    masking half of "input masking and data validation" happens here
    (single source of truth, rather than duplicating this in every
    caller): strips everything but digits, drops a leading "1" country
    code if present, then requires exactly 10 digits left. The client
    just displays whatever comes back in the response."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("Enter a 10-digit phone number.")
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


class ProfileInfo(BaseModel):
    email: str = ""
    email_notifications_enabled: bool = False
    sms_number: str = ""
    sms_notifications_enabled: bool = False
    # Whether the assistant may make changes in each of these areas
    # directly during a chat session, rather than only ever suggesting
    # them -- all off by default. Not yet enforced anywhere (see
    # auth_service/main.py's /profile endpoints' own docstring) -- persisted
    # preferences only for now.
    allow_configure_command_sets: bool = False
    allow_configure_apps: bool = False
    allow_configure_hosts: bool = False
    allow_configure_environments: bool = False
    allow_configure_local_agents: bool = False


class ProfileUpdateRequest(BaseModel):
    """PATCH /profile's body -- every field optional, so a caller can merge-
    update any subset (e.g. flip just one notification toggle) without
    resending the whole profile. Only the fields actually present in the
    request get merged into the stored profile (see main.py's
    update_profile)."""

    email: str | None = Field(default=None, max_length=254)
    email_notifications_enabled: bool | None = None
    sms_number: str | None = Field(default=None, max_length=32)
    sms_notifications_enabled: bool | None = None
    allow_configure_command_sets: bool | None = None
    allow_configure_apps: bool | None = None
    allow_configure_hosts: bool | None = None
    allow_configure_environments: bool | None = None
    allow_configure_local_agents: bool | None = None

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: str | None) -> str | None:
        if not value:
            return value
        return _validate_email_value(value)

    @field_validator("sms_number")
    @classmethod
    def _validate_sms_number(cls, value: str | None) -> str | None:
        if not value:
            return value
        return _validate_sms_number_value(value)
