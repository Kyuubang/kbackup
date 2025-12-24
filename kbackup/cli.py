import click
import logging
from typing import Optional, Tuple

from kbackup.backup import ClusterBackupService

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@click.group()
@click.version_option()
def cli():
    """Kubernetes manifest backup with well-structured folder"""
    pass


@cli.command(name="cluster")
@click.argument("context", required=True)
@click.option(
    "-f",
    "--filter",
    "jq_filter",
    help="jq filter syntax for manifest field filtering",
    default=".",
    show_default=True,
)
@click.option(
    "-e",
    "--exclude",
    "exclude_namespaces",
    multiple=True,
    help="namespace(s) to exclude from backup",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Perform a trial run with no changes made",
)
@click.option(
    "--dir",
    "output_dir",
    type=click.Path(file_okay=False),
    help="Directory for backup output (default: ./backup)",
)
@click.option(
    "--ingress-first",
    is_flag=True,
    help="Organize backup by ingress first, then backup deployments without ingress",
)
def cluster_backup(
    context: str,
    jq_filter: str,
    exclude_namespaces: Tuple[str, ...],
    dry_run: bool,
    output_dir: Optional[str],
    ingress_first: bool,
) -> None:
    """
    Backup Kubernetes cluster manifests.

    Backup all deployments and their associated resources from a Kubernetes cluster.
    Use --filter to customize manifest field filtering with jq syntax.
    Use --exclude to skip specific namespaces.
    Use --ingress-first to organize backup by ingress first.
    """
    try:
        backup_service = ClusterBackupService(
            context=context,
            jq_filter=jq_filter,
            exclude_namespaces=exclude_namespaces,
            output_dir=output_dir,
            ingress_first=ingress_first,
        )

        click.echo(f"Starting backup for cluster context: {context}")
        backup_service.backup(dry_run=dry_run)
        click.echo("Backup completed successfully!")

    except Exception as e:
        logger.error(f"Backup failed: {e}", exc_info=True)
        click.echo(f"Error: {e}", err=True)
        raise click.Exit(code=1)
