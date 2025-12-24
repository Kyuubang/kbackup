import logging
from pathlib import Path
from typing import Optional, Set, Tuple, Dict

from kubernetes import client, config
from kubernetes.client import ApiException

from kbackup.helper import (
    GetDeploymentAssociations,
    Serializer,
    YAMLFormatter,
    FileManager,
)

logger = logging.getLogger(__name__)


def _load_jq_filter(filter_input: str) -> str:
    """
    Load jq filter from a file or use it directly as a string.
    
    Args:
        filter_input: Either a jq filter string or a file path containing the filter
        
    Returns:
        The jq filter string
        
    Raises:
        FileNotFoundError: If filter_input is a file path that doesn't exist
        ValueError: If filter is empty
    """
    filter_path = Path(filter_input)
    
    # Check if it's a file that exists
    if filter_path.is_file():
        logger.info(f"Loading jq filter from file: {filter_path}")
        filter_content = filter_path.read_text(encoding='utf-8').strip()
        if not filter_content:
            raise ValueError(f"jq filter file is empty: {filter_path}")
        return filter_content
    
    # Otherwise treat it as a direct filter string
    if not filter_input.strip():
        raise ValueError("jq filter cannot be empty")
    
    return filter_input


class BackupConfig:
    """Configuration for cluster backup operations."""
    
    # Default namespaces to exclude from backup
    DEFAULT_EXCLUDE_NAMESPACES = {
        'kube-system',
        'kube-public',
        'kube-node-lease',
        'ingress-system',
        'gatekeeper-system',
        'portainer',
        'monitoring',
        'logging',
    }
    
    DEFAULT_OUTPUT_DIR = Path('./backup')


class KubernetesClientManager:
    """Manages Kubernetes API client initialization and configuration."""
    
    def __init__(self, context: str):
        """
        Initialize Kubernetes clients for the specified context.
        
        Args:
            context: Kubernetes context name
            
        Raises:
            Exception: If context loading fails
        """
        self.context = context
        self._load_context()
        self.v1 = client.CoreV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        self.apps_v1 = client.AppsV1Api()
        self.custom_api = client.CustomObjectsApi()
        self.autoscale_v1 = client.AutoscalingV1Api()
    
    def _load_context(self) -> None:
        """Load Kubernetes context."""
        try:
            config.load_kube_config(context=self.context)
            logger.info(f"Successfully loaded Kubernetes context: {self.context}")
        except Exception as e:
            logger.error(f"Failed to load context '{self.context}': {e}")
            raise


class ClusterBackupService:
    """Service for backing up Kubernetes cluster resources."""
    
    def __init__(
        self,
        context: str,
        jq_filter: str = ".",
        exclude_namespaces: Optional[Tuple[str, ...]] = None,
        output_dir: Optional[str] = None,
        ingress_first: bool = False,
    ):
        """
        Initialize the backup service.
        
        Args:
            context: Kubernetes context name
            jq_filter: jq filter string or path to file containing the filter
            exclude_namespaces: Tuple of namespaces to exclude
            output_dir: Output directory for backups
            ingress_first: If True, organize backup by ingress first
        """
        self.context = context
        self.jq_filter = _load_jq_filter(jq_filter)
        self.output_dir = Path(output_dir or BackupConfig.DEFAULT_OUTPUT_DIR) / context
        self.ingress_first = ingress_first
        
        # Combine default excludes with user-provided excludes
        self.exclude_namespaces: Set[str] = set(BackupConfig.DEFAULT_EXCLUDE_NAMESPACES)
        if exclude_namespaces:
            self.exclude_namespaces.update(exclude_namespaces)
        
        # Initialize Kubernetes clients
        self.k8s_clients = KubernetesClientManager(context)
        
        # Initialize helper services
        self.associations = GetDeploymentAssociations(
            self.k8s_clients.v1,
            self.k8s_clients.networking_v1,
            self.k8s_clients.autoscale_v1,
            self.k8s_clients.apps_v1,
        )
        
        self.serializer = Serializer(
            client,
            self.k8s_clients.v1,
            self.k8s_clients.networking_v1,
            self.k8s_clients.apps_v1,
            self.k8s_clients.custom_api,
            self.k8s_clients.autoscale_v1,
        )
        
        self.yaml_formatter = YAMLFormatter(logger=logger)
        self.file_manager = FileManager(logger=logger)
    
    def backup(self, dry_run: bool = False) -> None:
        """
        Execute the cluster backup.
        
        Args:
            dry_run: If True, only simulate the backup without writing files
            
        Raises:
            ApiException: If Kubernetes API calls fail
        """
        try:
            if not dry_run:
                self.file_manager.create_directory(self.output_dir)
                logger.info(f"Created backup directory: {self.output_dir}")
            
            # Track backed-up deployments to avoid duplicates
            backed_up_deployments: Set[Tuple[str, str]] = set()
            
            namespaces = self.k8s_clients.v1.list_namespace().items
            
            for ns in namespaces:
                if self.ingress_first:
                    self._backup_from_ingress(ns, dry_run=dry_run, backed_up_deployments=backed_up_deployments)
                
                self._backup_namespace(ns, dry_run=dry_run, backed_up_deployments=backed_up_deployments)
                
        except ApiException as e:
            logger.error(f"Kubernetes API error during backup: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error during backup: {e}")
            raise
    
    def _backup_from_ingress(self, namespace_obj, dry_run: bool = False, backed_up_deployments: Optional[Set[Tuple[str, str]]] = None) -> None:
        """
        Backup deployments organized by ingress, creating directory structure:
        namespace/ingress-name/ingress.yaml
        namespace/ingress-name/deployment-name/...
        """
        if backed_up_deployments is None:
            backed_up_deployments = set()
        
        ns_name = namespace_obj.metadata.name
        
        if ns_name in self.exclude_namespaces:
            return
        
        logger.info(f"Backing up from ingresses in namespace: {ns_name}")
        
        try:
            ingresses = self.k8s_clients.networking_v1.list_namespaced_ingress(namespace=ns_name).items
            
            for ingress in ingresses:
                ingress_name = ingress.metadata.name
                logger.info(f"Processing ingress: {ingress_name}")
                
                # Create ingress directory
                ingress_dir = self.output_dir / ns_name / ingress_name
                if not dry_run:
                    self.file_manager.create_directory(ingress_dir)
                
                # Backup ingress.yaml
                try:
                    ingress_data = self.serializer.get_ingress(ingress_name, ns_name)
                    if ingress_data:
                        ingress_yaml = self.yaml_formatter.format_kubernetes_resource(ingress_data, self.jq_filter)
                        if not dry_run:
                            ingress_file = ingress_dir / "ingress.yaml"
                            ingress_file.write_text(ingress_yaml, encoding='utf-8')
                            logger.debug(f"Wrote ingress file: {ingress_file}")
                except Exception as e:
                    logger.error(f"Failed to backup ingress {ingress_name}: {e}")
                
                # Extract service names from ingress rules
                service_names = set()
                if ingress.spec.rules:
                    for rule in ingress.spec.rules:
                        if rule.http and rule.http.paths:
                            for path in rule.http.paths:
                                if path.backend and path.backend.service:
                                    service_names.add(path.backend.service.name)
                
                # For each service, find and backup associated deployments
                for service_name in service_names:
                    try:
                        logger.info(f"Looking for deployments for service: {service_name}")
                        deployments = self.associations.get_deployments_for_service(service_name, ns_name)
                        if deployments:
                            logger.info(f"Found {len(deployments)} deployments for service {service_name}: {deployments}")
                            for deploy_name in deployments:
                                deploy_key = (ns_name, deploy_name)
                                if deploy_key not in backed_up_deployments:
                                    try:
                                        deploy = self.k8s_clients.apps_v1.read_namespaced_deployment(
                                            name=deploy_name, namespace=ns_name
                                        )
                                        # Backup deployment under ingress directory
                                        deploy_dir = ingress_dir / deploy_name
                                        logger.info(f"Backing up {deploy_name} to {deploy_dir}")
                                        self._backup_deployment_to_dir(deploy, ns_name, deploy_dir, dry_run=dry_run, backed_up_deployments=backed_up_deployments)
                                    except Exception as e:
                                        logger.warning(f"Failed to read deployment {deploy_name}: {e}")
                        else:
                            logger.warning(f"No deployments found for service {service_name}")
                    except Exception as e:
                        logger.warning(f"Failed to process service {service_name}: {e}")
                        
        except ApiException as e:
            logger.warning(f"Failed to list ingresses in namespace {ns_name}: {e}")
    
    def _backup_namespace(self, namespace_obj, dry_run: bool = False, backed_up_deployments: Optional[Set[Tuple[str, str]]] = None) -> None:
        """Backup all deployments in a namespace."""
        if backed_up_deployments is None:
            backed_up_deployments = set()
        
        ns_name = namespace_obj.metadata.name
        
        if ns_name in self.exclude_namespaces:
            logger.info(f"Skipping excluded namespace: {ns_name}")
            return
        
        logger.info(f"Processing namespace: {ns_name}")
        
        try:
            deployments = self.k8s_clients.apps_v1.list_namespaced_deployment(namespace=ns_name).items
            
            for deploy in deployments:
                deploy_key = (ns_name, deploy.metadata.name)
                if deploy_key not in backed_up_deployments:
                    deploy_dir = self.output_dir / ns_name / deploy.metadata.name
                    self._backup_deployment_to_dir(deploy, ns_name, deploy_dir, dry_run=dry_run, backed_up_deployments=backed_up_deployments)
                
        except ApiException as e:
            logger.error(f"Failed to list deployments in {ns_name}: {e}")
    
    def _backup_deployment_to_dir(self, deployment, namespace: str, deploy_dir: Path, dry_run: bool = False, backed_up_deployments: Optional[Set[Tuple[str, str]]] = None) -> None:
        """Backup deployment and all associated resources to a specific directory."""
        if backed_up_deployments is None:
            backed_up_deployments = set()
        
        deploy_name = deployment.metadata.name
        deploy_key = (namespace, deploy_name)
        
        # Mark as backed up
        backed_up_deployments.add(deploy_key)
        
        logger.info(f"Backing up deployment: {deploy_name} in namespace: {namespace}")
        
        if not dry_run:
            self.file_manager.create_directory(deploy_dir)
        
        resources = {}
        
        # Backup deployment
        try:
            deploy_data = self.serializer.get_deployment(deploy_name, namespace)
            deploy_yaml = self.yaml_formatter.format_kubernetes_resource(
                deploy_data, self.jq_filter
            )
            resources['deployment'] = deploy_yaml
        except Exception as e:
            logger.error(f"Failed to backup deployment {deploy_name}: {e}")
        
        # Backup associated resources
        try:
            service = self.associations.get_associated_service(deployment, namespace)
            if service:
                logger.info(f"Backing up service: {service} for deployment: {deploy_name}")
                service_data = self.serializer.get_service(service, namespace)
                service_yaml = self.yaml_formatter.format_kubernetes_resource(
                    service_data, self.jq_filter
                )
                resources['service'] = service_yaml
        except Exception as e:
            logger.warning(f"Failed to backup service for {deploy_name}: {e}")

        # backup associated ingress
        try:
            service_name = self.associations.get_associated_service(deployment, namespace)
            ingress = self.associations.get_associated_ingress(service_name, namespace)
            if ingress:
                logger.info(f"Backing up ingress: {ingress} for deployment: {deploy_name}")
                ingress_data = self.serializer.get_ingress(ingress, namespace)
                ingress_yaml = self.yaml_formatter.format_kubernetes_resource(
                    ingress_data, self.jq_filter
                )
                resources['ingress'] = ingress_yaml
        except Exception as e:
            logger.warning(f"Failed to backup ingress for {deploy_name}: {e}")
        
        # backup associated SecretProviderClass
        try:
            secret_providers = self.associations.get_associated_secretprovider(deployment)
            if secret_providers:
                for spc in secret_providers:
                    logger.info(f"Backing up SecretProviderClass: {spc['secret_provider_class']} for deployment: {deploy_name}")
                    spc_name = spc['secret_provider_class']
                    spc_data = self.serializer.get_secretprovider(spc_name, namespace)
                    spc_yaml = self.yaml_formatter.format_secret_provider_class(
                        spc_data, self.jq_filter
                    )
                    resources['secretprovider'] = spc_yaml
        except Exception as e:
            logger.warning(f"Failed to backup SecretProviderClass for {deploy_name}: {e}")
        
        # backup associated ConfigMaps
        try:
            configmaps = self.associations.get_associated_configmap(deployment)
            if configmaps:
                cm_yamls = []
                for cm_name in configmaps:
                    logger.info(f"Backing up ConfigMap: {cm_name} for deployment: {deploy_name}")
                    cm_data = self.serializer.get_configmap(cm_name, namespace)
                    cm_yaml = self.yaml_formatter.format_kubernetes_resource(
                        cm_data, self.jq_filter
                    )
                    cm_yamls.append(cm_yaml)
                resources['configmaps'] = "\n---\n".join(cm_yamls)
        except Exception as e:
            logger.warning(f"Failed to backup ConfigMaps for {deploy_name}: {e}")
        
        # backup associated secret 
        try:
            secrets = self.associations.get_associated_secret(deployment)
            if secrets:
                secret_yamls = []
                for secret_name in secrets:
                    logger.info(f"Backing up Secret: {secret_name} for deployment: {deploy_name}")
                    secret_data = self.serializer.get_secret(secret_name, namespace)
                    secret_yaml = self.yaml_formatter.format_kubernetes_resource(
                        secret_data, self.jq_filter
                    )
                    secret_yamls.append(secret_yaml)
                resources['secrets'] = "\n---\n".join(secret_yamls)
        except Exception as e:
            logger.warning(f"Failed to backup Secrets for {deploy_name}: {e}")
        
        # backup associated pvc
        try:
            pvcs = self.associations.get_associated_pvc(deployment)
            if pvcs:
                pvc_yamls = []
                for pvc_name in pvcs:
                    try:
                        logger.info(f"Backing up PVC: {pvc_name} for deployment: {deploy_name}")
                        pvc_data = self.serializer.get_pvc(pvc_name, namespace)
                        if pvc_data:
                            pvc_yaml = self.yaml_formatter.format_kubernetes_resource(
                                pvc_data, self.jq_filter
                            )
                            pvc_yamls.append(pvc_yaml)
                    except Exception as e:
                        logger.warning(f"Failed to backup PVC {pvc_name}: {e}")
                if pvc_yamls:
                    resources['pvc'] = "\n---\n".join(pvc_yamls)
        except Exception as e:
            logger.warning(f"Failed to backup PVCs for {deploy_name}: {e}")
        
        # backup associated hpa
        try:
            hpa = self.associations.get_associated_hpa(deployment)
            if hpa:
                logger.info(f"Backing up HPA: {hpa} for deployment: {deploy_name}")
                hpa_data = self.serializer.get_hpa(hpa, namespace)
                hpa_yaml = self.yaml_formatter.format_kubernetes_resource(
                    hpa_data, self.jq_filter
                )
                resources['hpa'] = hpa_yaml
        except Exception as e:
            logger.warning(f"Failed to backup HPA for {deploy_name}: {e}")
        
        # Write resources to files
        if not dry_run:
            self._write_resources(deploy_dir, resources)
    
    def _backup_deployment(self, deployment, namespace: str, dry_run: bool = False) -> None:
        """Backup a single deployment and its associated resources."""
        deploy_name = deployment.metadata.name
        deploy_dir = self.output_dir / namespace / deploy_name
        
        logger.info(f"Backing up deployment: {deploy_name} in namespace: {namespace}")
        
        if not dry_run:
            self.file_manager.create_directory(deploy_dir)
        
        resources = {}
        
        # Backup deployment
        try:
            deploy_data = self.serializer.get_deployment(deploy_name, namespace)
            deploy_yaml = self.yaml_formatter.format_kubernetes_resource(
                deploy_data, self.jq_filter
            )
            resources['deployment'] = deploy_yaml
        except Exception as e:
            logger.error(f"Failed to backup deployment {deploy_name}: {e}")
        
        # Backup associated service
        try:
            service = self.associations.get_associated_service(deployment, namespace)
            if service:
                logger.info(f"Backing up service: {service} for deployment: {deploy_name}")
                service_data = self.serializer.get_service(service, namespace)
                service_yaml = self.yaml_formatter.format_kubernetes_resource(
                    service_data, self.jq_filter
                )
                resources['service'] = service_yaml
        except Exception as e:
            logger.warning(f"Failed to backup service for {deploy_name}: {e}")
        
        # backup assciated ingress
        try:
            service_name = self.associations.get_associated_service(deployment, namespace)
            ingress = self.associations.get_associated_ingress(service_name, namespace)
            if ingress:
                logger.info(f"Backing up ingress: {ingress} for deployment: {deploy_name}")
                ingress_data = self.serializer.get_ingress(ingress, namespace)
                ingress_yaml = self.yaml_formatter.format_kubernetes_resource(
                    ingress_data, self.jq_filter
                )
                resources['ingress'] = ingress_yaml
        except Exception as e:
            logger.warning(f"Failed to backup ingress for {deploy_name}: {e}")
        
        # backup associated Secretprovider
        try:
            secret_providers = self.associations.get_associated_secretprovider(deployment)
            if secret_providers:
                for spc in secret_providers:
                    spc_name = spc['secret_provider_class']
                    logger.info(f"Backing up SecretProviderClass: {spc_name} for deployment: {deploy_name}")
                    spc_data = self.serializer.get_secretprovider(spc_name, namespace)
                    spc_yaml = self.yaml_formatter.format_secret_provider_class(
                        spc_data, self.jq_filter
                    )
                    resources['secretprovider'] = spc_yaml
        except Exception as e:
            logger.warning(f"Failed to backup SecretProviderClass for {deploy_name}: {e}")
        
        # backup associated ConfigMaps
        try:
            configmaps = self.associations.get_associated_configmap(deployment)
            if configmaps:
                cm_yamls = []
                for cm_name in configmaps:
                    logger.info(f"Backing up ConfigMap: {cm_name} for deployment: {deploy_name}")
                    cm_data = self.serializer.get_configmap(cm_name, namespace)
                    cm_yaml = self.yaml_formatter.format_kubernetes_resource(
                        cm_data, self.jq_filter
                    )
                    cm_yamls.append(cm_yaml)
                resources['configmaps'] = "\n---\n".join(cm_yamls)
        except Exception as e:
            logger.warning(f"Failed to backup ConfigMaps for {deploy_name}: {e}")
        
        # backup associated secret
        try:
            secrets = self.associations.get_associated_secret(deployment)
            if secrets:
                secret_yamls = []
                for secret_name in secrets:
                    logger.info(f"Backing up Secret: {secret_name} for deployment: {deploy_name}")
                    secret_data = self.serializer.get_secret(secret_name, namespace)
                    secret_yaml = self.yaml_formatter.format_kubernetes_resource(
                        secret_data, self.jq_filter
                    )
                    secret_yamls.append(secret_yaml)
                resources['secrets'] = "\n---\n".join(secret_yamls)
        except Exception as e:
            logger.warning(f"Failed to backup Secrets for {deploy_name}: {e}")
        
        # backup associated hpa
        try:
            hpa = self.associations.get_associated_hpa(deployment)
            if hpa:
                logger.info(f"Backing up HPA: {hpa} for deployment: {deploy_name}")
                hpa_data = self.serializer.get_hpa(hpa, namespace)
                hpa_yaml = self.yaml_formatter.format_kubernetes_resource(
                    hpa_data, self.jq_filter
                )
                resources['hpa'] = hpa_yaml
        except Exception as e:
            logger.warning(f"Failed to backup HPA for {deploy_name}: {e}")
        
        # Write resources to files
        if not dry_run:
            self._write_resources(deploy_dir, resources)
    
    def _write_resources(self, deploy_dir: Path, resources: Dict[str, str]) -> None:
        """Write backup resources to files."""
        for resource_type, yaml_content in resources.items():
            try:
                file_path = deploy_dir / f"{resource_type}.yaml"
                file_path.write_text(yaml_content, encoding='utf-8')
                logger.debug(f"Wrote resource file: {file_path}")
            except Exception as e:
                logger.error(f"Failed to write {resource_type}.yaml: {e}")

