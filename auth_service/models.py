import re

from pydantic import BaseModel, Field, field_validator


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


class HostInfo(BaseModel):
    host_id: int
    label: str
    hostname: str | None = None
    connected: bool
    local_agent_url: str | None = None
    workspace: list[str] = []
    command_key: str | None = None
    environment_ids: list[int] = []


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
