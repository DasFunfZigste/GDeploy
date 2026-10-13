"""Installer profiles captured with each verified ISO and deployment."""

UBUNTU = "ubuntu-autoinstall"
ALMA10 = "alma10-kickstart"
PROFILES = {
    UBUNTU: {"name": "Ubuntu Server 24.04 LTS", "architecture": "amd64", "installer": "autoinstall"},
    ALMA10: {"name": "AlmaLinux 10", "architecture": "x86_64", "installer": "Kickstart"},
}
CORELIGHT_ROLES = {"fleetmanager", "corelight_sensor"}
CORELIGHT_ALMA_WARNING = (
    "Experimental AlmaLinux 10: Corelight's 29.2 guides support Alma/RHEL 8 and 9, not 10. "
    "FleetManager uses its documented RPM repository; Software Sensor uses the documented EL 9 and "
    "rpm_any repositories. Package dependencies, service health and Fleet pairing must pass during installation. "
    "This is a lab test path, not vendor-certified AlmaLinux 10 support."
)


def profile_name(profile):
    return PROFILES.get(profile, {}).get("name", "Unknown operating system")
