import re
from typing import Literal

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
    # A set of addressable directories, not one fixed workspace -- may be
    # empty (nothing added on that host yet).
    workspace: list[str] = []


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
    NOT match blacklist). whitelist may instead be the literal reserved
    string "{roots}", meaning "must resolve to a path inside this host's
    own addressable directories" -- expanded by the Go daemon via its
    existing resolvePath/roots machinery, never compiled as a regex.

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

    @model_validator(mode="after")
    def _validate(self):
        if self.blacklist == "{roots}":
            raise ValueError('"{roots}" is only meaningful as a whitelist, not a blacklist.')
        for value in (self.whitelist, self.blacklist):
            if value == "{roots}":
                continue
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


def _pattern_uses_roots(pattern: Pattern) -> bool:
    return pattern.whitelist == "{roots}"


class PolicyLayerRuleCreateRequest(BaseModel):
    """One rule within a Policy Layer -- see policy_layer_rules' own
    schema comment in db.py for the full shape. positional_constraints'
    list index IS the argv position (index 0 = the binary). Position 0 is
    optional exactly like every other position -- an empty list, or a
    blank entry, both mean "value not required" there too; there's
    nothing special required about constraining the binary itself."""

    positional_constraints: list[Pattern] = Field(default_factory=list)
    option_constraints: list[OptionConstraint] = Field(default_factory=list)
    tier: Literal["allow", "ask", "deny"] = "ask"

    @model_validator(mode="after")
    def _validate_rule(self):
        if self.positional_constraints and _pattern_uses_roots(self.positional_constraints[0]):
            raise ValueError('Position 0 (the binary) can never use "{roots}" -- it is never a path.')
        uses_roots = any(_pattern_uses_roots(c) for c in self.positional_constraints) or any(
            _pattern_uses_roots(oc.pattern) for oc in self.option_constraints
        )
        if uses_roots and self.tier == "allow":
            raise ValueError(
                'A rule using "{roots}" cannot have tier "allow" -- only "ask" or "deny" '
                "(the client-side tier decision can only approximate directory containment; "
                "only the daemon can check it authoritatively)."
            )
        return self


class PolicyLayerRuleUpdateRequest(BaseModel):
    """PATCH /policy-layers/{id}/rules/{rule_id}'s body -- every field
    optional, same merge-only-what's-present convention as
    ProfileUpdateRequest. Cross-field validation (position 0, "{roots}"
    rules) is re-applied by main.py reconstructing the MERGED result
    through PolicyLayerRuleCreateRequest -- a partial patch can't be
    validated in isolation."""

    positional_constraints: list[Pattern] | None = None
    option_constraints: list[OptionConstraint] | None = None
    tier: Literal["allow", "ask", "deny"] | None = None


class PolicyLayerRuleInfo(BaseModel):
    id: int
    position: int
    positional_constraints: list[Pattern]
    option_constraints: list[OptionConstraint]
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
    composePolicy and the browser's own _compose_policy use) into one
    Policy and evaluates one hypothetical call against it. No side effects,
    no daemon involved -- the fast/deterministic bottom of the testing
    pyramid. roots and host_id are mutually exclusive: roots lets a caller
    test a "{roots}" rule with no real host needed at all; host_id instead
    derives it from one of the caller's own hosts' live, daemon-reported
    workspace (same posture as GET /hosts' own HostInfo.workspace -- empty
    for a host that isn't currently connected)."""

    policy_layer_ids: list[int] = Field(default_factory=list)
    positional_args: list[str] = Field(default_factory=list)
    options: list[PolicyEvalOption] = Field(default_factory=list)
    roots: list[str] | None = None
    host_id: int | None = None

    @model_validator(mode="after")
    def _roots_xor_host(self):
        if self.roots is not None and self.host_id is not None:
            raise ValueError("Specify at most one of roots/host_id, not both.")
        return self


class PolicyEvalResponse(BaseModel):
    tier: Literal["allow", "ask", "deny"]
    matched_layer_id: int | None = None
    matched_rule: PolicyLayerRuleInfo | None = None


class HostInfo(BaseModel):
    host_id: int
    label: str
    hostname: str | None = None
    connected: bool
    local_agent_url: str | None = None
    workspace: list[str] = []
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
    """POST /hosts/pending-approvals's body -- mirrors exactly what
    pages/chat.py already renders in its in-chat approval warning, so the
    two channels (native dialog vs. in-chat buttons) show the human the
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
    (single source of truth, rather than duplicating this in
    pages/settings_profile.py too): strips everything but digits, drops a
    leading "1" country code if present, then requires exactly 10 digits
    left. The client just displays whatever comes back in the response."""
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
    """PATCH /profile's body -- every field optional, since each of
    pages/settings_profile.py's/settings_security.py's controls saves
    itself independently on change rather than resending the whole profile
    just to flip one checkbox. Only the fields actually present in the
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
