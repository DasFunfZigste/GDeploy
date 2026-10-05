from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ssh_keys import MAX_SSH_KEY_BYTES, MAX_SSH_KEYS


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(StrictModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=1024)
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class AccountSetup(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    password: str = Field(min_length=12, max_length=1024)
    password_confirm: str = Field(min_length=12, max_length=1024)

    @model_validator(mode="after")
    def validate_new_credentials(self):
        if self.username.casefold() == "admin":
            raise ValueError("Choose a new administrator username other than admin.")
        if not self.password.strip():
            raise ValueError("Choose a password containing more than whitespace.")
        if self.password != self.password_confirm:
            raise ValueError("The password confirmation does not match.")
        return self


class CertificateHost(StrictModel):
    host: str = Field(min_length=1, max_length=253)

    @model_validator(mode="after")
    def validate_host(self):
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            if not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                for part in self.host.rstrip(".").split(".")
            ):
                raise ValueError("ESXi host must be a hostname or IP address, without a URL, port or path.")
        return self


class CertificateApproval(CertificateHost):
    fingerprint_sha256: str = Field(pattern=r"^(?:[A-Fa-f0-9]{2}:){31}[A-Fa-f0-9]{2}$")


class ConnectionSettings(CertificateHost):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(default="", max_length=1024)
    verify_tls: Literal[True] = True

    @field_validator("host", "username")
    @classmethod
    def trim_connection_fields(cls, value):
        return value.strip()


class SSHKeySettings(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, strict=True)
    public_keys: list[Annotated[str, Field(max_length=MAX_SSH_KEY_BYTES)]] = Field(max_length=MAX_SSH_KEYS)


class MediaSelection(StrictModel):
    media_id: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")


class SplunkPackageSelection(StrictModel):
    package_id: str = Field(min_length=1, max_length=1024)
    sha256: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{64}$")
    sha512: str | None = Field(default=None, pattern=r"^[A-Fa-f0-9]{128}$")

    @model_validator(mode="after")
    def validate_publisher_checksum(self):
        if (self.sha256 is None) == (self.sha512 is None):
            raise ValueError("Provide one publisher checksum: SHA-512 or SHA-256.")
        return self


class ESXiMediaSelection(CertificateHost):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    datastore: str = Field(min_length=1, max_length=128)
    path: str = Field(min_length=1, max_length=2048)
    sha256: str = Field(pattern=r"^[A-Fa-f0-9]{64}$")


class FleetManagerSettings(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False, strict=True)
    mode: Literal["online", "offline"]
    community_string: str = Field(default="", max_length=4096)
    repository_token: str = Field(default="", max_length=4096)
    license_pem: str = Field(default="", max_length=65536)
    license_name: str = Field(default="", max_length=200)
    package_id: str | None = Field(default=None, max_length=1024)
    dependency_ids: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(default_factory=list, max_length=128)


class VMSpec(StrictModel):
    role: Literal["ubuntu", "splunk", "elasticsearch", "kibana", "fleetmanager"]
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$")
    cpu: int = Field(ge=1, le=128, strict=True)
    ram_gb: int = Field(ge=2, le=2048, strict=True)
    disk_gb: int = Field(ge=25, le=65536, strict=True)
    datastore: str = Field(min_length=1, max_length=128)
    network: str = Field(min_length=1, max_length=128)
    ip_mode: Literal["dhcp", "static"] = "dhcp"
    address: str | None = None
    gateway: str | None = None
    dns: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_network(self):
        minimum = {"ubuntu": 2, "splunk": 4, "elasticsearch": 8, "kibana": 4, "fleetmanager": 8}[self.role]
        if self.ram_gb < minimum:
            raise ValueError(f"{self.role} requires at least {minimum} GiB RAM for this deployment profile.")
        if self.role == "fleetmanager" and (self.cpu < 2 or self.disk_gb < 60):
            raise ValueError("FleetManager requires at least 2 vCPUs and a 60 GiB disk; 80 GiB or more is recommended.")
        if self.ip_mode == "static":
            if not self.address or "/" not in self.address or not self.gateway or not self.dns:
                raise ValueError("Static networking requires IPv4 address/prefix, gateway and DNS servers.")
            interface = ipaddress.IPv4Interface(self.address)
            gateway = ipaddress.IPv4Address(self.gateway)
            if (
                interface.ip.is_loopback
                or interface.ip.is_multicast
                or interface.ip.is_unspecified
                or interface.ip.is_link_local
            ):
                raise ValueError("Choose a unicast, reachable IPv4 address.")
            if interface.network.prefixlen > 30 or interface.ip in (
                interface.network.network_address,
                interface.network.broadcast_address,
            ):
                raise ValueError("Choose a usable host address in a /1 through /30 subnet.")
            if (
                interface.network.prefixlen < 1
                or gateway not in interface.network
                or gateway in (interface.ip, interface.network.network_address, interface.network.broadcast_address)
            ):
                raise ValueError("Gateway must be another usable address in the VM subnet.")
            self.address = str(interface)
            self.gateway = str(gateway)
            self.dns = [str(ipaddress.IPv4Address(server)) for server in self.dns]
            if any(ipaddress.IPv4Address(s).is_unspecified or ipaddress.IPv4Address(s).is_multicast for s in self.dns):
                raise ValueError("DNS servers must be unicast IPv4 addresses.")
        else:
            self.address, self.gateway, self.dns = None, None, []
        return self


class DeploymentSpec(StrictModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
    vms: list[VMSpec] = Field(min_length=1, max_length=5)
    splunk_license_accepted: bool = False

    @model_validator(mode="after")
    def validate_topology(self):
        roles = [vm.role for vm in self.vms]
        if len(set(roles)) != len(roles):
            raise ValueError("Choose one VM per application role.")
        if len({vm.name for vm in self.vms}) != len(self.vms):
            raise ValueError("Each VM needs a unique name.")
        addresses = [str(ipaddress.IPv4Interface(vm.address).ip) for vm in self.vms if vm.address]
        if len(set(addresses)) != len(addresses):
            raise ValueError("Static IP addresses must be unique within a deployment.")
        if "kibana" in roles and "elasticsearch" not in roles:
            raise ValueError("Kibana requires a separate Elasticsearch VM in this deployment.")
        if "splunk" in roles and not self.splunk_license_accepted:
            raise ValueError("Confirm acceptance of the Splunk software license before deployment.")
        return self


class RedeployRequest(StrictModel):
    confirm_name: str
