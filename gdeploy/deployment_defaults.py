"""Host-bound defaults for new deployment forms; queued VM specs stay explicit."""

from .certificate_trust import certificate_endpoint
from .service import safe_error


class DeploymentDefaultsError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


class DeploymentDefaults:
    def __init__(self, db, service):
        self.db, self.service = db, service

    def catalog(self):
        connection = self.db.settings() or {}
        saved = self.db.deployment_defaults() or {}
        applicable = bool(
            saved and connection
            and saved.get("endpoint") == certificate_endpoint(connection["host"])[2]
        )
        return {
            "host": saved.get("host") or connection.get("host"),
            "default_network": saved.get("default_network"),
            "applies_to_host": applicable,
        }

    def save(self, host, network):
        with self.service.action_lock:
            connection = self.db.settings()
            if not connection:
                raise DeploymentDefaultsError("Save the ESXi connection before choosing a default port group.")
            endpoint = certificate_endpoint(host)[2]
            if endpoint != certificate_endpoint(connection["host"])[2]:
                raise DeploymentDefaultsError("The ESXi host changed. Reload its port groups before saving a default.", 409)
            try:
                with self.service.client(connection) as esxi:
                    inventory = esxi.inventory()
            except Exception as error:
                raise DeploymentDefaultsError(safe_error(error, connection)) from None
            if sum(item["name"] == network for item in inventory["networks"]) != 1:
                raise DeploymentDefaultsError("Choose an available, unambiguous port group from this ESXi host. Refresh its inventory and try again.")
            latest = self.db.settings()
            if not latest or certificate_endpoint(latest["host"])[2] != endpoint:
                raise DeploymentDefaultsError("The ESXi host changed. Reload its port groups before saving a default.", 409)
            self.db.set_deployment_defaults({
                "host": connection["host"], "endpoint": endpoint, "default_network": network,
            })
            return self.catalog()

    def clear(self):
        with self.service.action_lock:
            self.db.clear_deployment_defaults()
            return self.catalog()
