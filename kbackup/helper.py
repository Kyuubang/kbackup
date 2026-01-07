import json
import yaml
import jq
from pathlib import Path
from typing import Dict, List, Any, Union, Optional
import logging


class GetDeploymentAssociations:
    """Handles discovery of associated Kubernetes resources for deployments."""

    def __init__(
        self,
        v1_client: Any,
        networking_v1_client: Any,
        autoscale_v1_client: Any,
        apps_v1_client: Optional[Any] = None,
    ):
        """
        Initialize with Kubernetes API clients.

        Args:
            v1_client: CoreV1Api client
            networking_v1_client: NetworkingV1Api client
            autoscale_v1_client: AutoscalingV1Api client
            apps_v1_client: AppsV1Api client (optional)
        """
        self.v1 = v1_client
        self.networking_v1 = networking_v1_client
        self.autoscale_v1 = autoscale_v1_client
        self.apps = apps_v1_client

    def get_associated_service(self, deployment, namespace: str) -> Optional[str]:
        services = self.v1.list_namespaced_service(namespace=namespace)
        for svc in services.items:
            if svc.spec.selector == deployment.spec.selector.match_labels:
                return svc.metadata.name
        return None

    def get_associated_ingress(
        self, service: Optional[str], namespace: str
    ) -> Optional[str]:
        if not service:
            return None
        ingresses = self.networking_v1.list_namespaced_ingress(namespace=namespace)
        for ing in ingresses.items:
            for path in ing.spec.rules[0].http.paths:
                if service in path.backend.service.name:
                    return ing.metadata.name
        return None

    def get_associated_secretprovider(
        self, deployment
    ) -> Optional[List[Dict[str, str]]]:
        """
        Retrieve SecretProviderClass names from CSI volumes in the deployment.

        Args:
            deployment: Kubernetes Deployment object

        Returns:
            Optional[List[Dict[str, str]]]: List of dicts with volume name and SecretProviderClass name,
                                            or None if no volumes exist
        """
        volumes = deployment.spec.template.spec.volumes
        if not isinstance(volumes, list):
            return None

        result = []
        for volume in volumes:
            if getattr(volume, "csi", None) is not None:
                csi = volume.csi
                # Check if this is a secrets-store CSI driver
                if getattr(csi, "driver", None) == "secrets-store.csi.k8s.io":
                    # Get the actual secretProviderClass name from volume_attributes
                    spc_name = None
                    if (
                        getattr(csi, "volume_attributes", None)
                        and "secretProviderClass" in csi.volume_attributes
                    ):
                        spc_name = csi.volume_attributes["secretProviderClass"]

                    result.append(
                        {"volume_name": volume.name, "secret_provider_class": spc_name}
                    )

        return result if result else None

    def get_associated_hpa(self, deployment) -> Optional[str]:
        """
        Retrieve the name of the HorizontalPodAutoscaler associated with the given deployment.

        Args:
            deployment: Kubernetes Deployment object

        Returns:
            Optional[str]: Name of the associated HPA, or None if not found
        """
        namespace = deployment.metadata.namespace
        deployment_name = deployment.metadata.name

        # List all HPAs in the namespace
        hpas = self.autoscale_v1.list_namespaced_horizontal_pod_autoscaler(
            namespace=namespace
        )

        for hpa in hpas.items:
            if (
                hpa.spec.scale_target_ref.kind == "Deployment"
                and hpa.spec.scale_target_ref.name == deployment_name
            ):
                return hpa.metadata.name

        return None

    def get_associated_configmap(self, deployment) -> Optional[List[str]]:
        """
        Retrieve names of ConfigMaps associated with the given deployment within volumes, envFrom, and env.
        """
        containers = deployment.spec.template.spec.containers

        volumes = deployment.spec.template.spec.volumes
        if not isinstance(volumes, list):
            return None

        result = []
        for volume in volumes:
            if getattr(volume, "config_map", None) is not None:
                result.append(volume.config_map.name)

        for container in containers:
            env_from = container.env_from
            if not isinstance(env_from, list):
                continue
            for env in env_from:
                if getattr(env, "config_map_ref", None) is not None:
                    result.append(env.config_map_ref.name)
            
            env = container.env
            if not isinstance(env, list):
                continue
            for env_var in env:
                if getattr(env_var.value_from, "config_map_key_ref", None) is not None:
                    result.append(env_var.value_from.config_map_key_ref.name)

        return set(result) if result else None

    def get_associated_secret(self, deployment) -> Optional[List[str]]:
        """
        Retrieve names of Secrets associated with the given deployment within volumes, envFrom, and env.
        """
        containers = deployment.spec.template.spec.containers

        volumes = deployment.spec.template.spec.volumes
        if not isinstance(volumes, list):
            return None

        result = []
        for volume in volumes:
            if getattr(volume, "secret", None) is not None:
                result.append(volume.secret.secret_name)

        for container in containers:
            env_from = container.env_from
            if not isinstance(env_from, list):
                continue
            for env in env_from:
                if getattr(env, "secret_ref", None) is not None:
                    result.append(env.secret_ref.name)
            
            env = container.env
            if not isinstance(env, list):
                continue
            for env_var in env:
                if getattr(env_var.value_from, "secret_key_ref", None) is not None:
                    result.append(env_var.value_from.secret_key_ref.name)

        return set(result) if result else None

    def get_associated_pvc(self, deployment) -> Optional[List[str]]:
        """
        Retrieve names of PersistentVolumeClaims associated with the given deployment.

        Args:
            deployment: Kubernetes Deployment object

        Returns:
            Optional[List[str]]: List of PVC names, or None if no PVCs exist
        """
        volumes = deployment.spec.template.spec.volumes
        if not isinstance(volumes, list):
            return None

        result = []
        for volume in volumes:
            if getattr(volume, "persistent_volume_claim", None) is not None:
                result.append(volume.persistent_volume_claim.claim_name)

        return result if result else None

    def get_deployments_for_service(
        self, service_name: str, namespace: str
    ) -> Optional[List[str]]:
        """
        Find all deployments that target a specific service by matching selector labels.

        Args:
            service_name: Service name to find deployments for
            namespace: Kubernetes namespace

        Returns:
            Optional[List[str]]: List of deployment names, or None if none found
        """
        try:
            # Get the service to find its selector labels
            service = self.v1.read_namespaced_service(
                name=service_name, namespace=namespace
            )
            if not service.spec.selector:
                return None

            service_selector = service.spec.selector

            # List all deployments and find those matching the service selector
            deployments = self.apps.list_namespaced_deployment(namespace=namespace)

            matched_deployments = []
            for deploy in deployments.items:
                if deploy.spec.selector.match_labels == service_selector:
                    matched_deployments.append(deploy.metadata.name)

            return matched_deployments if matched_deployments else None

        except Exception:
            return None


class Serializer:
    """Handles serialization of Kubernetes resources to dictionary format."""

    def __init__(
        self,
        kubeclient: Any,
        v1_client: Any,
        networking_v1_client: Any,
        apps_client: Any,
        custom_client: Optional[Any] = None,
        autoscale_v1_client: Optional[Any] = None,
    ):
        """
        Initialize with Kubernetes API clients.

        Args:
            kubeclient: Main Kubernetes client
            v1_client: CoreV1Api client
            networking_v1_client: NetworkingV1Api client
            apps_client: AppsV1Api client
            custom_client: CustomObjectsApi client (optional)
        """
        self.v1 = v1_client
        self.networking_v1 = networking_v1_client
        self.apps = apps_client
        self.kubeclient = kubeclient
        self.custom = custom_client
        self.autoscale = autoscale_v1_client

    def _kubernetes_serializer(self, obj: Any) -> Any:
        return self.kubeclient.ApiClient().sanitize_for_serialization(obj)

    def get_service(
        self, service_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not service_name:
            return None
        svc = self.v1.read_namespaced_service(name=service_name, namespace=namespace)
        if svc:
            return self._kubernetes_serializer(svc)
        return None

    def get_ingress(
        self, ingress_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not ingress_name:
            return None
        ing = self.networking_v1.read_namespaced_ingress(
            name=ingress_name, namespace=namespace
        )
        if ing:
            return self._kubernetes_serializer(ing)
        return None

    def get_deployment(
        self, deployment_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not deployment_name:
            return None
        dep = self.apps.read_namespaced_deployment(
            name=deployment_name, namespace=namespace
        )
        if dep:
            return self._kubernetes_serializer(dep)
        return None

    def get_configmap(
        self, configmap_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not configmap_name:
            return None
        cm = self.v1.read_namespaced_config_map(
            name=configmap_name, namespace=namespace
        )
        if cm:
            return self._kubernetes_serializer(cm)
        return None

    def get_secret(
        self, secret_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not secret_name:
            return None
        secret = self.v1.read_namespaced_secret(name=secret_name, namespace=namespace)
        if secret:
            return self._kubernetes_serializer(secret)
        return None

    def get_secretprovider(
        self, name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not name or not self.custom:
            return None
        try:
            spc = self.custom.get_namespaced_custom_object(
                group="secrets-store.csi.x-k8s.io",
                version="v1",
                namespace=namespace,
                plural="secretproviderclasses",
                name=name,
            )
            if spc:
                return spc
        except Exception as e:
            print(f"Error retrieving SecretProviderClass {name}: {e}")
        return None

    def get_hpa(
        self, hpa_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        if not hpa_name:
            return None
        hpa = self.autoscale.read_namespaced_horizontal_pod_autoscaler(
            name=hpa_name, namespace=namespace
        )
        if hpa:
            return self._kubernetes_serializer(hpa)
        return None

    def get_pvc(
        self, pvc_name: Optional[str], namespace: str
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve a PersistentVolumeClaim by name.

        Args:
            pvc_name: PVC name
            namespace: Kubernetes namespace

        Returns:
            Optional[Dict[str, Any]]: Serialized PVC data, or None if not found
        """
        if not pvc_name:
            return None
        try:
            pvc = self.v1.read_namespaced_persistent_volume_claim(
                name=pvc_name, namespace=namespace
            )
            if pvc:
                return self._kubernetes_serializer(pvc)
        except Exception:
            return None
        return None


class YAMLFormatter:
    """
    Handles YAML formatting with support for block-style multi-line strings.

    This class provides methods to format Kubernetes resources and other data structures
    into clean, readable YAML with proper block-style formatting for multi-line strings.
    """

    class BlockStyleDumper(yaml.SafeDumper):
        """Custom YAML dumper that formats multi-line strings using block style (|)."""

        def represent_str(self, data: str) -> yaml.ScalarNode:
            """
            Represent strings with newlines using block style formatting.

            Args:
                data: String to represent

            Returns:
                yaml.ScalarNode: YAML scalar node with appropriate style
            """
            if "\n" in data:
                return self.represent_scalar("tag:yaml.org,2002:str", data, style="|")
            return self.represent_scalar("tag:yaml.org,2002:str", data)

    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize the YAML formatter.

        Args:
            logger: Optional logger instance for error reporting
        """
        self.logger = logger or logging.getLogger(__name__)

        # Configure the custom dumper
        self.BlockStyleDumper.add_representer(str, self.BlockStyleDumper.represent_str)

        # Default YAML dump options
        self._yaml_options = {
            "Dumper": self.BlockStyleDumper,
            "sort_keys": False,
            "default_flow_style": False,
        }

    def _apply_jq_filter(
        self, data: Union[Dict, List, Any], jq_filter: str
    ) -> Dict[str, Any]:
        """
        Apply jq filter to data.

        Args:
            data: Data to filter
            jq_filter: jq filter string

        Returns:
            Filtered data

        Raises:
            ValueError: If jq filter is invalid or fails
        """
        try:
            # Ensure data is JSON serializable
            if isinstance(data, (dict, list)):
                data_interface = data
            else:
                json_data = json.dumps(data)
                data_interface = json.loads(json_data)

            # Apply jq filter
            compiled_filter = jq.compile(jq_filter)
            result = compiled_filter.input(data_interface).first()

            if result is None:
                raise ValueError("jq filter returned no results")

            return result

        except Exception as e:
            self.logger.error(f"Failed to apply jq filter: {e}")
            raise ValueError(f"jq filter error: {e}") from e

    def _fix_multiline_strings(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Fix escaped newlines in multi-line strings recursively.

        Args:
            data: Data structure to process

        Returns:
            Data with fixed newlines
        """
        if isinstance(data, dict):
            return {k: self._fix_multiline_strings(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._fix_multiline_strings(item) for item in data]
        elif isinstance(data, str) and "\\n" in data:
            return data.replace("\\n", "\n")
        else:
            return data

    def _remove_empty_values(self, data: Any) -> Any:
        """
        Recursively remove empty dictionaries, empty lists, and None values.

        Args:
            data: Data structure to process

        Returns:
            Data with empty values removed
        """
        if isinstance(data, dict):
            # Filter out None values and empty dicts/lists, then recursively process remaining values
            cleaned = {
                k: self._remove_empty_values(v)
                for k, v in data.items()
                if v is not None  # Skip None values
            }
            # Remove entries where the value became empty after recursive cleaning
            return {
                k: v
                for k, v in cleaned.items()
                if not (isinstance(v, (dict, list)) and len(v) == 0)
            }
        elif isinstance(data, list):
            # Filter out None values and empty dicts/lists from lists
            return [
                self._remove_empty_values(item)
                for item in data
                if item is not None
                and not (isinstance(item, (dict, list)) and len(item) == 0)
            ]
        else:
            return data

    def format_kubernetes_resource(
        self, data: Union[Dict, List, Any], jq_filter: str
    ) -> str:
        """
        Format a general Kubernetes resource with jq filtering and YAML formatting.

        Args:
            data: Kubernetes resource data
            jq_filter: jq filter to clean up the resource

        Returns:
            Formatted YAML string

        Raises:
            RuntimeError: If formatting fails
        """
        try:
            # Apply jq filter
            result = self._apply_jq_filter(data, jq_filter)

            # Fix any escaped newlines
            result = self._fix_multiline_strings(result)

            # Remove empty values (annotations, labels, etc)
            result = self._remove_empty_values(result)

            # Convert to YAML
            yaml_data = yaml.dump(result, **self._yaml_options)
            return yaml_data

        except Exception as e:
            self.logger.error(f"Failed to format Kubernetes resource: {e}")
            raise RuntimeError(f"Error formatting resource: {e}") from e

    def format_secret_provider_class(
        self, spc_data: Dict[str, Any], jq_filter: str
    ) -> str:
        """
        Format SecretProviderClass with special handling for the objects field.

        The objects field in SecretProviderClass often contains YAML-like strings
        that need special processing to maintain proper block-style formatting.

        Args:
            spc_data: SecretProviderClass data
            jq_filter: jq filter to clean up the resource

        Returns:
            Formatted YAML string with proper block-style objects field

        Raises:
            RuntimeError: If formatting fails
        """
        try:
            # Apply jq filter
            result = self._apply_jq_filter(spc_data, jq_filter)

            # Special handling for SecretProviderClass objects field
            self._process_spc_objects_field(result)

            # Remove empty values (annotations, labels, etc)
            result = self._remove_empty_values(result)

            # Convert to YAML
            yaml_data = yaml.dump(result, **self._yaml_options)
            return yaml_data

        except Exception as e:
            self.logger.error(f"Failed to format SecretProviderClass: {e}")
            raise RuntimeError(f"Error formatting SecretProviderClass: {e}") from e

    def _process_spc_objects_field(self, spc_data: Dict[str, Any]) -> None:
        """
        Process the objects field in SecretProviderClass data to fix newlines.

        Args:
            spc_data: SecretProviderClass data to modify in-place
        """
        try:
            objects_path = spc_data.get("spec", {}).get("parameters", {})
            if "objects" in objects_path:
                objects_str = objects_path["objects"]
                if isinstance(objects_str, str) and "\\n" in objects_str:
                    objects_path["objects"] = objects_str.replace("\\n", "\n")
        except (KeyError, AttributeError) as e:
            self.logger.warning(f"Could not process objects field: {e}")

    def set_yaml_options(self, **options) -> None:
        """
        Update YAML formatting options.

        Args:
            **options: Keyword arguments to update YAML options
        """
        self._yaml_options.update(options)
        # Ensure we always use our custom dumper
        self._yaml_options["Dumper"] = self.BlockStyleDumper


class FileManager:
    """
    Handles file system operations for YAML files including directory creation and file writing.

    This class provides utilities for creating directory structures and writing YAML content
    to files with proper error handling and logging.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize FileManager with optional logger.

        Args:
            logger: Optional logger instance for debugging and monitoring
        """
        self.logger = logger or logging.getLogger(__name__)

    def create_directory(self, directory_path: Union[str, Path]) -> Path:
        """
        Create directory structure recursively if it doesn't exist.

        Args:
            directory_path: Path to the directory to create

        Returns:
            Path object of the created directory

        Raises:
            OSError: If directory creation fails due to permissions or other OS issues
            ValueError: If the path is invalid
        """
        try:
            path_obj = Path(directory_path)

            # Validate path
            if not str(path_obj).strip():
                raise ValueError("Directory path cannot be empty")

            # Create directory structure
            path_obj.mkdir(parents=True, exist_ok=True)

            # Try to log relative path, fallback to absolute if not possible
            try:
                display_path = path_obj.resolve().relative_to(Path.cwd().resolve())
            except ValueError:
                # Path is not relative to cwd, use absolute path
                display_path = path_obj.resolve()

            self.logger.info(f"Directory created/verified: {display_path}")
            return path_obj

        except Exception as e:
            self.logger.error(f"Failed to create directory {directory_path}: {e}")
            raise OSError(f"Cannot create directory {directory_path}: {e}") from e

    def write_yaml_file(
        self,
        file_path: Union[str, Path],
        data: Dict[str, Any],
        create_dirs: bool = True,
        yaml_formatter: Optional[YAMLFormatter] = None,
    ) -> Path:
        """
        Write data to a YAML file with optional directory creation.

        Args:
            file_path: Path to the YAML file to write
            data: Data to write to the file
            create_dirs: Whether to create parent directories if they don't exist
            yaml_formatter: Optional YAMLFormatter instance for custom formatting

        Returns:
            Path object of the written file

        Raises:
            OSError: If file writing fails due to permissions or other OS issues
            ValueError: If the file path or data is invalid
            RuntimeError: If YAML formatting fails
        """
        try:
            file_path_obj = Path(file_path)

            # Validate inputs
            if not str(file_path_obj).strip():
                raise ValueError("File path cannot be empty")
            if not isinstance(data, dict):
                raise ValueError("Data must be a dictionary")

            # Create parent directories if requested
            if create_dirs and file_path_obj.parent != file_path_obj:
                self.create_directory(file_path_obj.parent)

            # Format YAML content
            if yaml_formatter:
                yaml_content = yaml_formatter.format_kubernetes_resource(data, ".")
            else:
                # Use default YAML formatting
                yaml_content = yaml.dump(
                    data, default_flow_style=False, sort_keys=False
                )

            # Write to file
            file_path_obj.write_text(yaml_content, encoding="utf-8")

            self.logger.info(f"YAML file written: {file_path_obj.absolute()}")
            return file_path_obj

        except Exception as e:
            self.logger.error(f"Failed to write YAML file {file_path}: {e}")
            raise OSError(f"Cannot write YAML file {file_path}: {e}") from e

    def write_multiple_yaml_files(
        self,
        file_data_map: Dict[Union[str, Path], Dict[str, Any]],
        base_directory: Optional[Union[str, Path]] = None,
        create_dirs: bool = True,
        yaml_formatter: Optional[YAMLFormatter] = None,
    ) -> List[Path]:
        """
        Write multiple YAML files in batch operation.

        Args:
            file_data_map: Dictionary mapping file paths to data to write
            base_directory: Optional base directory to prepend to relative paths
            create_dirs: Whether to create parent directories if they don't exist
            yaml_formatter: Optional YAMLFormatter instance for custom formatting

        Returns:
            List of Path objects for successfully written files

        Raises:
            RuntimeError: If any file writing fails, includes details of all failures
        """
        written_files = []
        failed_files = []

        try:
            for file_path, data in file_data_map.items():
                try:
                    # Handle base directory
                    if base_directory:
                        file_path = Path(base_directory) / file_path

                    written_file = self.write_yaml_file(
                        file_path=file_path,
                        data=data,
                        create_dirs=create_dirs,
                        yaml_formatter=yaml_formatter,
                    )
                    written_files.append(written_file)

                except Exception as e:
                    failed_files.append((file_path, str(e)))
                    self.logger.error(f"Failed to write {file_path}: {e}")

            if failed_files:
                failure_details = ", ".join(
                    [f"{path}: {error}" for path, error in failed_files]
                )
                raise RuntimeError(
                    f"Failed to write {len(failed_files)} files: {failure_details}"
                )

            self.logger.info(f"Successfully wrote {len(written_files)} YAML files")
            return written_files

        except Exception as e:
            self.logger.error(f"Batch YAML file writing failed: {e}")
            raise

    def ensure_yaml_extension(self, file_path: Union[str, Path]) -> Path:
        """
        Ensure the file path has a .yaml extension.

        Args:
            file_path: Original file path

        Returns:
            Path object with .yaml extension
        """
        path_obj = Path(file_path)
        if path_obj.suffix.lower() not in [".yaml", ".yml"]:
            path_obj = path_obj.with_suffix(".yaml")
        return path_obj

    def create_resource_directory_structure(
        self, base_path: Union[str, Path], namespace: str, resource_types: List[str]
    ) -> Dict[str, Path]:
        """
        Create a standard directory structure for Kubernetes resources.

        Creates directories like: base_path/namespace/deployments/, base_path/namespace/services/, etc.

        Args:
            base_path: Base directory path
            namespace: Kubernetes namespace name
            resource_types: List of resource types (e.g., ['deployments', 'services', 'configmaps'])

        Returns:
            Dictionary mapping resource types to their directory paths

        Raises:
            OSError: If directory creation fails
            ValueError: If inputs are invalid
        """
        try:
            if not namespace.strip():
                raise ValueError("namespace cannot be empty")
            if not resource_types:
                raise ValueError("resource_types cannot be empty")

            base_path_obj = Path(base_path)
            namespace_path = base_path_obj / namespace

            directory_map = {}

            for resource_type in resource_types:
                if not resource_type.strip():
                    continue

                resource_dir = namespace_path / resource_type
                created_dir = self.create_directory(resource_dir)
                directory_map[resource_type] = created_dir

            self.logger.info(
                f"Created directory structure for namespace '{namespace}' with {len(directory_map)} resource types"
            )
            return directory_map

        except Exception as e:
            self.logger.error(f"Failed to create resource directory structure: {e}")
            raise

    def create_cluster_backup_structure(
        self,
        base_path: Union[str, Path],
        cluster_config: Dict[str, Dict[str, List[str]]],
    ) -> Dict[str, Dict[str, Dict[str, Path]]]:
        """
        Create directory structure for cluster backup organized by cluster/namespace/application.

        Structure: target_backup_dir/cluster_name/namespace/application_name/

        Args:
            base_path: Base directory for backup
            cluster_config: Dictionary structure like:
                {
                    "amazonlily": {
                        "api": ["api-dealer"],
                        "chatbot": ["api-messages", "messages-consumer", "webhook"],
                        "frontend": ["appinsight", "catalog-web", "chat-console", "cms", "landing-page", "salesappweb"]
                    },
                    "cluster-n": {
                        "namespace1": ["app1", "app2"]
                    }
                }

        Returns:
            Nested dictionary mapping cluster -> namespace -> application -> Path

        Raises:
            OSError: If directory creation fails
            ValueError: If cluster_config structure is invalid
        """
        try:
            if not cluster_config:
                raise ValueError("cluster_config cannot be empty")

            base_path_obj = Path(base_path)
            directory_structure = {}

            for cluster_name, namespaces in cluster_config.items():
                if not cluster_name.strip():
                    self.logger.warning("Skipping empty cluster name")
                    continue

                if not isinstance(namespaces, dict):
                    raise ValueError(
                        f"Namespaces for cluster '{cluster_name}' must be a dictionary"
                    )

                cluster_path = base_path_obj / cluster_name
                directory_structure[cluster_name] = {}

                for namespace, applications in namespaces.items():
                    if not namespace.strip():
                        self.logger.warning(
                            f"Skipping empty namespace in cluster '{cluster_name}'"
                        )
                        continue

                    if not isinstance(applications, list):
                        raise ValueError(
                            f"Applications for namespace '{namespace}' must be a list"
                        )

                    namespace_path = cluster_path / namespace
                    directory_structure[cluster_name][namespace] = {}

                    for application in applications:
                        if not application.strip():
                            self.logger.warning(
                                f"Skipping empty application name in {cluster_name}/{namespace}"
                            )
                            continue

                        app_path = namespace_path / application
                        created_dir = self.create_directory(app_path)
                        directory_structure[cluster_name][namespace][
                            application
                        ] = created_dir

                        self.logger.debug(
                            f"Created application directory: {created_dir}"
                        )

            total_dirs = sum(
                len(apps)
                for cluster in directory_structure.values()
                for apps in cluster.values()
            )
            self.logger.info(
                f"Created cluster backup structure with {len(directory_structure)} clusters, {total_dirs} application directories"
            )

            return directory_structure

        except Exception as e:
            self.logger.error(f"Failed to create cluster backup structure: {e}")
            raise

    def write_application_resources(
        self,
        base_path: Union[str, Path],
        cluster_name: str,
        namespace: str,
        application: str,
        resources: Dict[str, Dict[str, Any]],
        yaml_formatter: Optional[YAMLFormatter] = None,
    ) -> List[Path]:
        """
        Write Kubernetes resources for a specific application to the cluster backup structure.

        Args:
            base_path: Base backup directory
            cluster_name: Name of the cluster
            namespace: Kubernetes namespace
            application: Application name
            resources: Dictionary mapping resource types to resource data
                      e.g., {"deployment": deployment_data, "service": service_data, "ingress": ingress_data}
            yaml_formatter: Optional YAMLFormatter for custom formatting

        Returns:
            List of created file paths

        Raises:
            OSError: If file writing fails
            ValueError: If inputs are invalid
        """
        try:
            if not all([cluster_name.strip(), namespace.strip(), application.strip()]):
                raise ValueError(
                    "cluster_name, namespace, and application cannot be empty"
                )
            if not resources:
                raise ValueError("resources cannot be empty")

            # Build application directory path
            app_dir = Path(base_path) / cluster_name / namespace / application

            # Ensure directory exists
            self.create_directory(app_dir)

            written_files = []

            for resource_type, resource_data in resources.items():
                if not resource_type.strip():
                    continue

                # Create filename with .yaml extension
                filename = self.ensure_yaml_extension(f"{resource_type}")
                file_path = app_dir / filename

                # Write the resource file
                written_file = self.write_yaml_file(
                    file_path=file_path,
                    data=resource_data,
                    create_dirs=False,  # Directory already created
                    yaml_formatter=yaml_formatter,
                )
                written_files.append(written_file)

            self.logger.info(
                f"Wrote {len(written_files)} resource files for {cluster_name}/{namespace}/{application}"
            )
            return written_files

        except Exception as e:
            self.logger.error(f"Failed to write application resources: {e}")
            raise

    def create_backup_from_config_file(
        self, base_path: Union[str, Path], config_file_path: Union[str, Path]
    ) -> Dict[str, Dict[str, Dict[str, Path]]]:
        """
        Create cluster backup structure from a JSON/YAML configuration file.

        Expected config file format (JSON or YAML):
        {
            "clusters": {
                "amazonlily": {
                    "api": ["api-dealer"],
                    "chatbot": ["api-messages", "messages-consumer", "webhook"],
                    "frontend": ["appinsight", "catalog-web", "chat-console", "cms", "landing-page", "salesappweb"]
                }
            }
        }

        Args:
            base_path: Base directory for backup
            config_file_path: Path to configuration file (JSON or YAML)

        Returns:
            Directory structure mapping

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If config file format is invalid
            RuntimeError: If parsing fails
        """
        try:
            config_path = Path(config_file_path)

            if not config_path.exists():
                raise FileNotFoundError(f"Configuration file not found: {config_path}")

            # Read and parse config file
            config_content = config_path.read_text(encoding="utf-8")

            if config_path.suffix.lower() in [".yaml", ".yml"]:
                config_data = yaml.safe_load(config_content)
            elif config_path.suffix.lower() == ".json":
                config_data = json.loads(config_content)
            else:
                # Try both formats
                try:
                    config_data = yaml.safe_load(config_content)
                except yaml.YAMLError:
                    config_data = json.loads(config_content)

            # Validate config structure
            if not isinstance(config_data, dict) or "clusters" not in config_data:
                raise ValueError(
                    "Config file must contain a 'clusters' key with cluster definitions"
                )

            clusters_config = config_data["clusters"]
            if not isinstance(clusters_config, dict):
                raise ValueError("'clusters' must be a dictionary")

            # Create directory structure
            return self.create_cluster_backup_structure(base_path, clusters_config)

        except Exception as e:
            self.logger.error(f"Failed to create backup from config file: {e}")
            raise