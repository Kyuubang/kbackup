import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch, mock_open
from click.testing import CliRunner

from kbackup.cli import cli
from kbackup.backup import (
    _load_jq_filter,
    _parse_namespaces,
    _process_values,
    BackupConfig,
    KubernetesClientManager,
    ClusterBackupService,
)
from kbackup.helper import (
    GetDeploymentAssociations,
    Serializer,
    YAMLFormatter,
    FileManager,
)


# ===== CLI Tests =====


def test_version():
    """Test version command output."""
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.startswith("cli, version ")


def test_cluster_command_help():
    """Test cluster command help displays correctly."""
    runner = CliRunner()
    result = runner.invoke(cli, ["cluster", "--help"])
    assert result.exit_code == 0
    assert "Backup Kubernetes cluster manifests" in result.output
    assert "--filter" in result.output
    assert "--exclude" in result.output
    assert "--dry-run" in result.output
    assert "--dir" in result.output
    assert "--ingress-first" in result.output


def test_cluster_command_requires_context():
    """Test cluster command requires context argument."""
    runner = CliRunner()
    result = runner.invoke(cli, ["cluster"])
    assert result.exit_code == 2
    assert "Missing argument 'CONTEXT'" in result.output


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_context(mock_service_class):
    """Test cluster command with context argument."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cluster", "test-context"])
        
        # Should attempt to create service and call backup
        mock_service_class.assert_called_once()
        mock_service.backup.assert_called_once_with(dry_run=False)


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_dry_run(mock_service_class):
    """Test cluster command with --dry-run flag."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cluster", "test-context", "--dry-run"])
        
        mock_service.backup.assert_called_once_with(dry_run=True)


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_filter(mock_service_class):
    """Test cluster command with custom jq filter."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["cluster", "test-context", "-f", ".metadata | {name, namespace}"]
        )
        
        # Check that filter was passed to service
        call_kwargs = mock_service_class.call_args[1]
        assert call_kwargs["jq_filter"] == ".metadata | {name, namespace}"


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_exclude_namespaces(mock_service_class):
    """Test cluster command with excluded namespaces."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["cluster", "test-context", "-e", "kube-system", "-e", "monitoring"],
        )
        
        call_kwargs = mock_service_class.call_args[1]
        assert "kube-system" in call_kwargs["exclude_namespaces"]
        assert "monitoring" in call_kwargs["exclude_namespaces"]


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_comma_separated_excludes(mock_service_class):
    """Test cluster command with comma-separated exclude values."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli,
            ["cluster", "test-context", "-e", "ns1,ns2,ns3"],
        )
        
        call_kwargs = mock_service_class.call_args[1]
        # Should receive the comma-separated string
        assert "ns1,ns2,ns3" in call_kwargs["exclude_namespaces"]


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_exclude_file(mock_service_class):
    """Test cluster command with exclude file using @ prefix."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        # Create exclude file
        exclude_file = Path("exclude.txt")
        exclude_file.write_text("ns1 ns2\nns3, ns4")
        
        result = runner.invoke(
            cli,
            ["cluster", "test-context", "-e", "@exclude.txt"],
        )
        
        call_kwargs = mock_service_class.call_args[1]
        # Should receive the file reference with @ prefix
        assert "@exclude.txt" in call_kwargs["exclude_namespaces"]


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_custom_output_dir(mock_service_class):
    """Test cluster command with custom output directory."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(
            cli, ["cluster", "test-context", "--dir", "./my-backup"]
        )
        
        call_kwargs = mock_service_class.call_args[1]
        assert call_kwargs["output_dir"] == "./my-backup"


@patch("kbackup.cli.ClusterBackupService")
def test_cluster_command_with_ingress_first(mock_service_class):
    """Test cluster command with --ingress-first flag."""
    runner = CliRunner()
    mock_service = Mock()
    mock_service_class.return_value = mock_service

    with runner.isolated_filesystem():
        result = runner.invoke(cli, ["cluster", "test-context", "--ingress-first"])
        
        call_kwargs = mock_service_class.call_args[1]
        assert call_kwargs["ingress_first"] is True


# ===== Backup Module Tests =====


def test_load_jq_filter_from_string():
    """Test loading jq filter from a direct string."""
    filter_str = ".metadata | {name, namespace}"
    result = _load_jq_filter(filter_str)
    assert result == filter_str


def test_load_jq_filter_from_file(tmp_path):
    """Test loading jq filter from a file."""
    filter_file = tmp_path / "filter.txt"
    filter_content = ".metadata | {name, namespace, labels}"
    filter_file.write_text(filter_content)

    result = _load_jq_filter(str(filter_file))
    assert result == filter_content


def test_load_jq_filter_empty_string():
    """Test that empty jq filter raises ValueError."""
    with pytest.raises(ValueError, match="jq filter cannot be empty"):
        _load_jq_filter("   ")


def test_load_jq_filter_empty_file(tmp_path):
    """Test that empty jq filter file raises ValueError."""
    filter_file = tmp_path / "empty_filter.txt"
    filter_file.write_text("   ")

    with pytest.raises(ValueError, match="jq filter file is empty"):
        _load_jq_filter(str(filter_file))


def test_load_jq_filter_nonexistent_file():
    """Test that non-existent file is treated as a filter string."""
    filter_str = "nonexistent.txt"
    result = _load_jq_filter(filter_str)
    assert result == filter_str


def test_parse_exclude_namespaces_space_delimited():
    """Test parsing space-delimited namespaces."""
    content = "ns1 ns2 ns3"
    result = _parse_namespaces(content)
    assert result == {"ns1", "ns2", "ns3"}


def test_parse_exclude_namespaces_comma_delimited():
    """Test parsing comma-delimited namespaces."""
    content = "ns1, ns2, ns3"
    result = _parse_namespaces(content)
    assert result == {"ns1", "ns2", "ns3"}


def test_parse_exclude_namespaces_newline_delimited():
    """Test parsing newline-delimited namespaces."""
    content = "ns1\nns2\nns3"
    result = _parse_namespaces(content)
    assert result == {"ns1", "ns2", "ns3"}


def test_parse_exclude_namespaces_mixed_delimiters():
    """Test parsing mixed delimiters."""
    content = """# Space delimited
kube-system kube-public

# Comma delimited
monitoring, logging, ingress-system

# One per line
istio-system
prometheus

# Mixed
grafana loki, jaeger
"""
    result = _parse_namespaces(content)
    expected = {
        "kube-system", "kube-public", "monitoring", "logging",
        "ingress-system", "istio-system", "prometheus", 
        "grafana", "loki", "jaeger"
    }
    assert result == expected


def test_parse_exclude_namespaces_with_comments():
    """Test parsing properly filters out comments."""
    content = """# This is a comment
ns1 ns2
# Another comment
ns3, ns4
"""
    result = _parse_namespaces(content)
    # Comments should be filtered out
    assert result == {"ns1", "ns2", "ns3", "ns4"}
    assert "#" not in result
    assert "comment" not in result


def test_parse_exclude_namespaces_with_inline_comments():
    """Test parsing with inline comments."""
    content = """ns1 ns2  # inline comment
ns3, ns4  # another inline comment
ns5  # comment after single namespace
"""
    result = _parse_namespaces(content)
    assert result == {"ns1", "ns2", "ns3", "ns4", "ns5"}
    assert "inline" not in result
    assert "comment" not in result


def test_process_exclude_values_single():
    """Test processing single exclude values."""
    result = _process_values(("ns1", "ns2"))
    assert result == {"ns1", "ns2"}


def test_process_exclude_values_comma_separated():
    """Test processing comma-separated values."""
    result = _process_values(("ns1,ns2,ns3",))
    assert result == {"ns1", "ns2", "ns3"}


def test_process_exclude_values_mixed():
    """Test processing mixed single and comma-separated values."""
    result = _process_values(("ns1", "ns2,ns3", "ns4"))
    assert result == {"ns1", "ns2", "ns3", "ns4"}


def test_process_exclude_values_file_reference(tmp_path):
    """Test processing file reference with @ prefix."""
    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text("ns1 ns2\nns3, ns4")
    
    result = _process_values((f"@{exclude_file}",))
    assert result == {"ns1", "ns2", "ns3", "ns4"}


def test_process_exclude_values_file_and_inline(tmp_path):
    """Test processing combination of file reference and inline values."""
    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text("file-ns1 file-ns2")
    
    result = _process_values(("cli-ns1", f"@{exclude_file}", "cli-ns2,cli-ns3"))
    assert result == {"cli-ns1", "cli-ns2", "cli-ns3", "file-ns1", "file-ns2"}


def test_process_exclude_values_nonexistent_file():
    """Test that non-existent file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError, match="Exclude file not found"):
        _process_values(("@nonexistent.txt",))


def test_process_exclude_values_empty_file(tmp_path):
    """Test that empty file raises ValueError."""
    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text("   ")
    
    with pytest.raises(ValueError, match="Exclude file is empty"):
        _process_values((f"@{exclude_file}",))


def test_process_exclude_values_file_with_no_valid_namespaces(tmp_path):
    """Test file with only whitespace and delimiters."""
    exclude_file = tmp_path / "exclude.txt"
    exclude_file.write_text("  ,  \n  ,  ")
    
    with pytest.raises(ValueError, match="No valid namespaces found"):
        _process_values((f"@{exclude_file}",))


def test_backup_config_default_excludes():
    """Test default excluded namespaces."""
    assert "kube-system" in BackupConfig.DEFAULT_EXCLUDE_NAMESPACES
    assert "kube-public" in BackupConfig.DEFAULT_EXCLUDE_NAMESPACES
    assert "monitoring" in BackupConfig.DEFAULT_EXCLUDE_NAMESPACES


def test_backup_config_default_output_dir():
    """Test default output directory."""
    assert BackupConfig.DEFAULT_OUTPUT_DIR == Path("./backup")


@patch("kbackup.backup.config")
@patch("kbackup.backup.client")
def test_kubernetes_client_manager_initialization(mock_client, mock_config):
    """Test KubernetesClientManager initializes all clients."""
    manager = KubernetesClientManager("test-context")

    mock_config.load_kube_config.assert_called_once_with(context="test-context")
    assert manager.context == "test-context"
    assert manager.v1 is not None
    assert manager.networking_v1 is not None
    assert manager.apps_v1 is not None
    assert manager.custom_api is not None
    assert manager.autoscale_v1 is not None


@patch("kbackup.backup.config")
def test_kubernetes_client_manager_load_context_failure(mock_config):
    """Test KubernetesClientManager raises exception on context load failure."""
    mock_config.load_kube_config.side_effect = Exception("Context not found")

    with pytest.raises(Exception, match="Context not found"):
        KubernetesClientManager("invalid-context")


@patch("kbackup.backup.KubernetesClientManager")
@patch("kbackup.backup._load_jq_filter")
def test_cluster_backup_service_initialization(mock_load_filter, mock_k8s_manager):
    """Test ClusterBackupService initialization."""
    mock_load_filter.return_value = "."
    
    service = ClusterBackupService(
        context="test-context",
        jq_filter=".",
        exclude_namespaces=("custom-ns",),
        output_dir="./test-backup",
        ingress_first=True,
    )

    assert service.context == "test-context"
    assert service.jq_filter == "."
    assert service.ingress_first is True
    assert "custom-ns" in service.exclude_namespaces
    assert "kube-system" in service.exclude_namespaces
    assert service.output_dir == Path("./test-backup/test-context")


@patch("kbackup.backup.KubernetesClientManager")
@patch("kbackup.backup._load_jq_filter")
def test_cluster_backup_service_default_values(mock_load_filter, mock_k8s_manager):
    """Test ClusterBackupService with default values."""
    mock_load_filter.return_value = "."
    
    service = ClusterBackupService(context="test-context")

    assert service.jq_filter == "."
    assert service.ingress_first is False
    assert service.output_dir == Path("./backup/test-context")
    assert service.exclude_namespaces == BackupConfig.DEFAULT_EXCLUDE_NAMESPACES


# ===== Helper Module Tests =====


def test_get_deployment_associations_initialization():
    """Test GetDeploymentAssociations initialization."""
    v1_mock = Mock()
    networking_mock = Mock()
    autoscale_mock = Mock()
    apps_mock = Mock()

    assoc = GetDeploymentAssociations(
        v1_mock, networking_mock, autoscale_mock, apps_mock
    )

    assert assoc.v1 == v1_mock
    assert assoc.networking_v1 == networking_mock
    assert assoc.autoscale_v1 == autoscale_mock
    assert assoc.apps == apps_mock


def test_serializer_initialization():
    """Test Serializer initialization."""
    kube_mock = Mock()
    v1_mock = Mock()
    networking_mock = Mock()
    apps_mock = Mock()
    custom_mock = Mock()
    autoscale_mock = Mock()

    serializer = Serializer(
        kube_mock, v1_mock, networking_mock, apps_mock, custom_mock, autoscale_mock
    )

    assert serializer.kubeclient == kube_mock
    assert serializer.v1 == v1_mock
    assert serializer.networking_v1 == networking_mock
    assert serializer.apps == apps_mock
    assert serializer.custom == custom_mock
    assert serializer.autoscale == autoscale_mock


def test_yaml_formatter_initialization():
    """Test YAMLFormatter initialization."""
    formatter = YAMLFormatter()
    assert formatter is not None
    assert formatter._yaml_options["sort_keys"] is False
    assert formatter._yaml_options["default_flow_style"] is False


def test_yaml_formatter_fix_multiline_strings():
    """Test fixing escaped newlines in strings."""
    formatter = YAMLFormatter()
    data = {"config": "line1\\nline2\\nline3"}
    
    result = formatter._fix_multiline_strings(data)
    
    assert result["config"] == "line1\nline2\nline3"


def test_yaml_formatter_remove_empty_values():
    """Test removing empty values from data."""
    formatter = YAMLFormatter()
    data = {
        "name": "test",
        "empty_dict": {},
        "empty_list": [],
        "none_value": None,
        "nested": {
            "value": "kept",
            "empty": {},
        },
    }
    
    result = formatter._remove_empty_values(data)
    
    assert "name" in result
    assert "empty_dict" not in result
    assert "empty_list" not in result
    assert "none_value" not in result
    assert "empty" not in result["nested"]


def test_file_manager_initialization():
    """Test FileManager initialization."""
    manager = FileManager()
    assert manager is not None
    assert manager.logger is not None


def test_file_manager_create_directory(tmp_path):
    """Test directory creation."""
    manager = FileManager()
    test_dir = tmp_path / "test" / "nested" / "dir"
    
    result = manager.create_directory(test_dir)
    
    assert result.exists()
    assert result.is_dir()
    assert result == test_dir


def test_file_manager_create_directory_already_exists(tmp_path):
    """Test creating directory that already exists."""
    manager = FileManager()
    test_dir = tmp_path / "existing"
    test_dir.mkdir()
    
    result = manager.create_directory(test_dir)
    
    assert result.exists()
    assert result == test_dir


def test_file_manager_create_directory_empty_path():
    """Test that empty path creates current directory."""
    manager = FileManager()
    
    # Empty string creates '.' which is valid
    result = manager.create_directory("")
    assert result.exists()


def test_file_manager_ensure_yaml_extension():
    """Test ensuring YAML extension."""
    manager = FileManager()
    
    assert manager.ensure_yaml_extension("file.txt").suffix == ".yaml"
    assert manager.ensure_yaml_extension("file.yaml").suffix == ".yaml"
    assert manager.ensure_yaml_extension("file.yml").suffix == ".yml"
    assert manager.ensure_yaml_extension("file").suffix == ".yaml"


def test_file_manager_write_yaml_file(tmp_path):
    """Test writing YAML file."""
    manager = FileManager()
    file_path = tmp_path / "test.yaml"
    data = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "test-pod"}}
    
    result = manager.write_yaml_file(file_path, data, create_dirs=False)
    
    assert result.exists()
    assert "test-pod" in result.read_text()


def test_file_manager_write_yaml_file_with_directory_creation(tmp_path):
    """Test writing YAML file with directory creation."""
    manager = FileManager()
    file_path = tmp_path / "nested" / "dir" / "test.yaml"
    data = {"kind": "Service"}
    
    result = manager.write_yaml_file(file_path, data, create_dirs=True)
    
    assert result.exists()
    assert result.parent.exists()


def test_file_manager_create_resource_directory_structure(tmp_path):
    """Test creating resource directory structure."""
    manager = FileManager()
    resource_types = ["deployments", "services", "configmaps"]
    
    result = manager.create_resource_directory_structure(
        tmp_path, "default", resource_types
    )
    
    assert len(result) == 3
    assert (tmp_path / "default" / "deployments").exists()
    assert (tmp_path / "default" / "services").exists()
    assert (tmp_path / "default" / "configmaps").exists()


def test_file_manager_create_cluster_backup_structure(tmp_path):
    """Test creating cluster backup structure."""
    manager = FileManager()
    cluster_config = {
        "cluster1": {
            "namespace1": ["app1", "app2"],
            "namespace2": ["app3"],
        },
        "cluster2": {
            "namespace3": ["app4"],
        },
    }
    
    result = manager.create_cluster_backup_structure(tmp_path, cluster_config)
    
    assert len(result) == 2
    assert "cluster1" in result
    assert "namespace1" in result["cluster1"]
    assert (tmp_path / "cluster1" / "namespace1" / "app1").exists()
    assert (tmp_path / "cluster1" / "namespace1" / "app2").exists()
    assert (tmp_path / "cluster2" / "namespace3" / "app4").exists()


def test_file_manager_write_application_resources(tmp_path):
    """Test writing application resources."""
    manager = FileManager()
    resources = {
        "deployment": {"kind": "Deployment", "metadata": {"name": "app"}},
        "service": {"kind": "Service", "metadata": {"name": "app-svc"}},
    }
    
    result = manager.write_application_resources(
        tmp_path, "cluster1", "default", "app1", resources
    )
    
    assert len(result) == 2
    assert (tmp_path / "cluster1" / "default" / "app1" / "deployment.yaml").exists()
    assert (tmp_path / "cluster1" / "default" / "app1" / "service.yaml").exists()


# ===== Integration-style Tests =====


@patch("kbackup.backup.KubernetesClientManager")
@patch("kbackup.backup._load_jq_filter")
def test_backup_service_creates_output_directory(mock_load_filter, mock_k8s_manager, tmp_path):
    """Test that backup service creates output directory."""
    mock_load_filter.return_value = "."
    mock_k8s = MagicMock()
    mock_k8s_manager.return_value = mock_k8s
    
    # Mock namespace list to be empty
    mock_k8s.v1.list_namespace.return_value.items = []
    
    output_dir = tmp_path / "test-backup"
    service = ClusterBackupService(
        context="test-context",
        output_dir=str(output_dir)
    )
    
    service.backup(dry_run=False)
    
    assert service.output_dir.exists()


@patch("kbackup.backup.KubernetesClientManager")
@patch("kbackup.backup._load_jq_filter")
def test_backup_service_dry_run_no_files(mock_load_filter, mock_k8s_manager, tmp_path):
    """Test that dry run doesn't create files."""
    mock_load_filter.return_value = "."
    mock_k8s = MagicMock()
    mock_k8s_manager.return_value = mock_k8s
    
    mock_k8s.v1.list_namespace.return_value.items = []
    
    output_dir = tmp_path / "test-backup"
    service = ClusterBackupService(
        context="test-context",
        output_dir=str(output_dir)
    )
    
    service.backup(dry_run=True)
    
    # Output directory should not exist in dry run
    assert not service.output_dir.exists()


@patch("kbackup.backup.KubernetesClientManager")
@patch("kbackup.backup._load_jq_filter")
def test_backup_service_excludes_namespaces(mock_load_filter, mock_k8s_manager, tmp_path):
    """Test that excluded namespaces are skipped."""
    mock_load_filter.return_value = "."
    mock_k8s = MagicMock()
    mock_k8s_manager.return_value = mock_k8s
    
    # Create mock namespaces
    ns1 = Mock()
    ns1.metadata.name = "kube-system"
    ns2 = Mock()
    ns2.metadata.name = "default"
    
    mock_k8s.v1.list_namespace.return_value.items = [ns1, ns2]
    mock_k8s.apps_v1.list_namespaced_deployment.return_value.items = []
    
    service = ClusterBackupService(
        context="test-context",
        output_dir=str(tmp_path)
    )
    
    service.backup(dry_run=False)
    
    # kube-system should be excluded, only default should be processed
    call_args_list = mock_k8s.apps_v1.list_namespaced_deployment.call_args_list
    namespaces_processed = [call[1]["namespace"] for call in call_args_list]
    
    assert "default" in namespaces_processed
    assert "kube-system" not in namespaces_processed
