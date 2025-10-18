"""
Cisco Hardware EoX Data Synchronization Command

Sync hardware lifecycle information from Cisco's EoX Support API.
"""

import requests
import json
import django.utils.text

from dataclasses import dataclass
from datetime import datetime, date
from functools import partial
from enum import Enum
from collections.abc import Callable

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import MultipleObjectsReturned
from django.contrib.contenttypes.models import ContentType

from dcim.models import Device, DeviceType, Module, ModuleType, Manufacturer  # type: ignore
from netbox_lifecycle.models import hardware

class HardwareType(Enum):
    """Hardware type enumeration."""
    DEVICE_TYPE = "devicetype"
    MODULE_TYPE = "moduletype"


@dataclass(frozen=True)
class PluginConfig:
    """Plugin configuration container."""
    track_only_active_pids: bool
    api_is_source_of_truth: bool
    set_missing_data_as_end_of_support: bool
    cisco_client_id: str
    cisco_client_secret: str


@dataclass(frozen=True)
class EoXDates:
    """Container for End-of-X dates."""
    end_of_sale: date | None = None
    end_of_maintenance: date | None = None
    end_of_security: date | None = None
    end_of_support: date | None = None
    last_contract_date: date | None = None


@dataclass
class HardwareInfo:
    """Container for hardware information."""
    pid: str
    hardware_type: HardwareType
    hw_obj: object
    content_type: ContentType
    active_count: int


def parse_date(date_string: str) -> date | None:
    """
    Parse a date string in YYYY-MM-DD format.

    Args:
        date_string: Date string to parse

    Returns:
        Parsed date or None if invalid
    """
    try:
        return datetime.strptime(date_string, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def extract_date_from_eox(
    eox_data: dict[str, object],
    field_path: list[str]
) -> date | None:
    """
    Extract and parse a date from nested EoX data structure.

    Args:
        eox_data: The EoX API response data
        field_path: Path to the field (e.g., ["EOXRecord", 0, "EndOfSaleDate", "value"])

    Returns:
        Parsed date or None if not found/invalid
    """
    try:
        value = eox_data
        for key in field_path:
            value = value[key]

        if not value:
            return None

        return parse_date(value) if isinstance(value, str) else None
    except (KeyError, IndexError, TypeError):
        return None


def extract_eox_dates(eox_data: dict[str, object]) -> EoXDates:
    """
    Extract all EoX dates from API response data.

    Args:
        eox_data: The EoX API response data

    Returns:
        EoXDates object with all extracted dates
    """
    field_mappings = {
        'end_of_sale': ['EOXRecord', 0, 'EndOfSaleDate', 'value'],
        'end_of_maintenance': ['EOXRecord', 0, 'EndOfSWMaintenanceReleases', 'value'],
        'end_of_security': ['EOXRecord', 0, 'EndOfSecurityVulSupportDate', 'value'],
        'end_of_support': ['EOXRecord', 0, 'LastDateOfSupport', 'value'],
        'last_contract_date': ['EOXRecord', 0, 'EndOfServiceContractRenewal', 'value'],
    }

    extracted_dates = {
        field_name: extract_date_from_eox(eox_data, field_path)
        for field_name, field_path in field_mappings.items()
    }

    return EoXDates(**extracted_dates)


def should_track_hardware(active_count: int, track_only_active: bool) -> bool:
    """
    Determine if hardware should be tracked based on active count.

    Args:
        active_count: Number of active hardware instances
        track_only_active: Whether to track only active PIDs

    Returns:
        True if hardware should be tracked
    """
    return not track_only_active or active_count > 0


def compute_lifecycle_updates(
    current_lifecycle: hardware.HardwareLifecycle,
    new_dates: EoXDates,
    set_missing_as_eol: bool
) -> tuple[dict[str, object], bool]:
    """
    Compute what updates need to be made to lifecycle object.

    Args:
        current_lifecycle: Current lifecycle object
        new_dates: New dates from API
        set_missing_as_eol: Whether to set missing dates to end_of_support

    Returns:
        Tuple of (updates_dict, has_required_fields)
    """
    updates = {}
    if new_dates.end_of_sale and current_lifecycle.end_of_sale != new_dates.end_of_sale:
        updates['end_of_sale'] = new_dates.end_of_sale

    if new_dates.end_of_maintenance and current_lifecycle.end_of_maintenance != new_dates.end_of_maintenance:
        updates['end_of_maintenance'] = new_dates.end_of_maintenance

    if new_dates.end_of_security and current_lifecycle.end_of_security != new_dates.end_of_security:
        updates['end_of_security'] = new_dates.end_of_security

    if new_dates.end_of_support and current_lifecycle.end_of_support != new_dates.end_of_support:
        updates['end_of_support'] = new_dates.end_of_support

    has_required_fields = bool(new_dates.end_of_sale and new_dates.end_of_support)
    if set_missing_as_eol and has_required_fields and new_dates.end_of_support:
        if not new_dates.end_of_security and not current_lifecycle.end_of_security:
            updates['end_of_security'] = new_dates.end_of_support
        if not new_dates.end_of_maintenance and not current_lifecycle.end_of_maintenance:
            updates['end_of_maintenance'] = new_dates.end_of_support

    return updates, has_required_fields


@dataclass(frozen=True)
class HardwareTypeConfig:
    """Configuration for a hardware type."""
    model_class: type
    instance_model: type
    content_type_model: str
    filter_field: str


def get_hardware_type_config(hw_type: HardwareType) -> HardwareTypeConfig:
    """
    Get configuration for a specific hardware type.

    Args:
        hw_type: The hardware type

    Returns:
        Configuration for that hardware type
    """
    configs = {
        HardwareType.DEVICE_TYPE: HardwareTypeConfig(
            model_class=DeviceType,
            instance_model=Device,
            content_type_model="devicetype",
            filter_field="device_type"
        ),
        HardwareType.MODULE_TYPE: HardwareTypeConfig(
            model_class=ModuleType,
            instance_model=Module,
            content_type_model="moduletype",
            filter_field="module_type"
        )
    }
    return configs[hw_type]


def get_hardware_object(
    pid: str,
    hw_type: HardwareType
) -> tuple[object | None, ContentType | None, int]:
    """
    Retrieve hardware object, content type, and active count.

    Args:
        pid: Product ID
        hw_type: Hardware type

    Returns:
        Tuple of (hardware_obj, content_type, active_count)
        Returns (None, None, 0) if multiple objects found or error
    """
    config = get_hardware_type_config(hw_type)

    try:
        hw_obj = config.model_class.objects.get(part_number=pid)
        content_type = ContentType.objects.get(app_label="dcim", model=config.content_type_model)

        filter_kwargs = {config.filter_field: hw_obj}
        active_count = config.instance_model.objects.filter(**filter_kwargs).count()

        return hw_obj, content_type, active_count

    except MultipleObjectsReturned:
        return None, None, 0
    except config.model_class.DoesNotExist:
        return None, None, 0


def load_plugin_config() -> PluginConfig:
    """
    Load plugin configuration from Django settings.

    Returns:
        PluginConfig object with all settings
    """
    plugin_settings = settings.PLUGINS_CONFIG.get("netbox_lifecycle", {})

    return PluginConfig(
        track_only_active_pids=plugin_settings.get("track_only_active_pids", True),
        api_is_source_of_truth=plugin_settings.get("api_is_source_of_truth", True),
        set_missing_data_as_end_of_support=plugin_settings.get("set_missing_data_as_end_of_support", True),
        cisco_client_id=plugin_settings.get("cisco_support_api_client_id", ""),
        cisco_client_secret=plugin_settings.get("cisco_support_api_client_secret", "")
    )


def get_api_token(client_id: str, client_secret: str) -> str | None:
    """
    Authenticate with Cisco API and retrieve access token.

    Args:
        client_id: Cisco API client ID
        client_secret: Cisco API client secret

    Returns:
        Access token or None if authentication fails
    """
    token_url = "https://id.cisco.com/oauth2/default/v1/token"
    data = {
        'grant_type': 'client_credentials',
        'client_id': client_id,
        'client_secret': client_secret
    }

    try:
        response = requests.post(token_url, data=data)
        response.raise_for_status()
        tokens = response.json()
        return tokens.get('access_token')
    except (requests.RequestException, KeyError, json.JSONDecodeError):
        return None
    


def create_api_headers(access_token: str) -> dict[str, str]:
    """
    Create API request headers with authentication.

    Args:
        access_token: API access token

    Returns:
        Dictionary of headers
    """
    return {
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json'
    }



def fetch_eox_data(pid: str, headers: dict[str, str]) -> dict[str, object] | None:
    """
    Fetch EoX data for a product ID from Cisco API.

    Args:
        pid: Product ID
        headers: API request headers

    Returns:
        EoX data dictionary or None if request fails
    """
    url = f'https://apix.cisco.com/supporttools/eox/rest/5/EOXByProductID/1/{pid}?responseencoding=json'

    try:
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            return response.json()
        return None
    except (requests.RequestException, json.JSONDecodeError):
        return None


def collect_hardware_pids(
    manufacturer: Manufacturer,
    model_class: type,
    hw_type: HardwareType,
    log_fn: Callable[[str, str], None]
) -> dict[str, HardwareType]:
    """
    Collect product IDs for a specific hardware type.

    Args:
        manufacturer: Manufacturer object
        model_class: Model class to query (DeviceType or ModuleType)
        hw_type: Hardware type enum value
        log_fn: Logging function

    Returns:
        Dictionary mapping PIDs to hardware types
    """
    results = {}

    try:
        items = model_class.objects.filter(manufacturer=manufacturer)
        for item in items:
            if not item.part_number:
                log_fn('WARNING', f'Found {hw_type.value} "{item}" WITHOUT Part Number - SKIPPING')
                continue

            log_fn('SUCCESS', f'Found {hw_type.value} "{item}" with Part Number "{item.part_number}"')
            results[item.part_number] = hw_type

    except model_class.DoesNotExist:
        pass

    return results


def get_all_product_ids(
    manufacturer_name: str,
    log_fn: Callable[[str, str], None]
) -> dict[str, HardwareType]:
    """
    Get all product IDs for a manufacturer.

    Args:
        manufacturer_name: Name of the manufacturer
        log_fn: Logging function

    Returns:
        Dictionary mapping PIDs to hardware types

    Raises:
        CommandError: If manufacturer not found
    """
    try:
        manufacturer = Manufacturer.objects.get(name=manufacturer_name)
    except Manufacturer.DoesNotExist:
        raise CommandError(f'Manufacturer "{manufacturer_name}" does not exist')

    log_fn('SUCCESS', f'Found manufacturer "{manufacturer}"')

    device_pids = collect_hardware_pids(
        manufacturer, DeviceType, HardwareType.DEVICE_TYPE, log_fn
    )
    module_pids = collect_hardware_pids(
        manufacturer, ModuleType, HardwareType.MODULE_TYPE, log_fn
    )

    return {**device_pids, **module_pids}


class Command(BaseCommand):
    """Sync Hardware Lifecycle Information from Cisco EoX Support API."""

    help = 'Sync Hardware Lifecycle Information from Cisco EoX Support API'


    def add_arguments(self, parser):
        parser.add_argument(
            '--manufacturer',
            default='Cisco',
            help='Manufacturer name (default: Cisco)',
        )

    

    def log(self, level: str, message: str) -> None:
        """
        Log a message with appropriate styling.

        Args:
            level: Log level (SUCCESS, WARNING, ERROR, NOTICE)
            message: Message to log
        """
        style_map = {
            'SUCCESS': self.style.SUCCESS,
            'WARNING': self.style.WARNING,
            'ERROR': self.style.ERROR,
            'NOTICE': self.style.NOTICE,
        }
        style_fn = style_map.get(level, lambda x: x)
        self.stdout.write(style_fn(message))


    def get_or_create_lifecycle(
        self,
        hw_info: HardwareInfo,
        config: PluginConfig
    ) -> hardware.HardwareLifecycle | None:
        """
        Get existing or create new lifecycle record.

        Args:
            hw_info: Hardware information
            config: Plugin configuration

        Returns:
            HardwareLifecycle object or None if shouldn't be tracked
        """
        try:
            hw_lifecycle = hardware.HardwareLifecycle.objects.get(
                assigned_object_id=hw_info.hw_obj.id
            )
            self.log('SUCCESS', f"{hw_info.pid} - has an existing NetBox hardware lifecycle record")

            if hw_info.active_count == 0 and config.track_only_active_pids:
                self.log('NOTICE',
                    f"{hw_info.pid} - has no active hardware with this PID - "
                    f"We're tracking only active PIDs - Deleting Lifecycle record")
                hw_lifecycle.delete()
                return None

            return hw_lifecycle

        except hardware.HardwareLifecycle.DoesNotExist:
            if not should_track_hardware(hw_info.active_count, config.track_only_active_pids):
                self.log('NOTICE',
                    f"{hw_info.pid} - no active hardware with this PID - "
                    f"We're only tracking active PIDs - no Lifecycle record created")
                return None

            self.log('NOTICE', f"{hw_info.pid} - has no existing NetBox hardware lifecycle record")
            return hardware.HardwareLifecycle(
                assigned_object_id=hw_info.hw_obj.id,
                assigned_object_type_id=hw_info.content_type.id
            )
        

    def update_lifecycle_record(
        self,
        pid: str,
        hw_type: HardwareType,
        eox_data: dict[str, object],
        config: PluginConfig
    ) -> None:
        """
        Update lifecycle data for a product ID.

        Args:
            pid: Product ID
            hw_type: Hardware type
            eox_data: EoX data from API
            config: Plugin configuration
        """
        self.log('SUCCESS', f"{pid} - {hw_type.value}")

        hw_obj, content_type, active_count = get_hardware_object(pid, hw_type)

        if hw_obj is None:
            self.log('NOTICE', f"ERROR: Multiple objects exist with Part Number {pid}")
            return

        hw_info = HardwareInfo(pid, hw_type, hw_obj, content_type, active_count)
        self.log('SUCCESS', f"{pid} - {active_count} active {hw_type.value}s")

        hw_lifecycle = self.get_or_create_lifecycle(hw_info, config)
        if hw_lifecycle is None:
            return

        new_dates = extract_eox_dates(eox_data)
        date_fields = [
            ('end_of_sale', 'end_of_sale_date'),
            ('end_of_maintenance', 'end_of_sw_maintenance_releases'),
            ('end_of_security', 'end_of_security_vul_support_date'),
            ('end_of_support', 'last_date_of_support'),
        ]

        for field_name, display_name in date_fields:
            date_value = getattr(new_dates, field_name)
            if date_value:
                self.log('SUCCESS', f"{pid} - {display_name}: {date_value}")
            else:
                self.log('NOTICE', f"{pid} - has no {display_name}")

        updates, has_required = compute_lifecycle_updates(
            hw_lifecycle,
            new_dates,
            config.set_missing_data_as_end_of_support
        )
        if updates and has_required:
            for field, value in updates.items():
                setattr(hw_lifecycle, field, value)
            hw_lifecycle.save()
            self.log('SUCCESS', f"{pid} - Lifecycle record updated")


    def handle(self, *args, **kwargs):
        """Main entry point for the command."""
        manufacturer = kwargs.get('manufacturer', 'Cisco')

        config = load_plugin_config()

        access_token = get_api_token(config.cisco_client_id, config.cisco_client_secret)
        if not access_token:
            raise CommandError("Failed to authenticate with Cisco API")

        api_headers = create_api_headers(access_token)

        product_ids = get_all_product_ids(manufacturer, self.log)
        self.log('SUCCESS', f'Querying API for these PIDs: {", ".join(product_ids)}')

        for pid, hw_type in product_ids.items():
            self.log('SUCCESS', '#' * 55)

            eox_data = fetch_eox_data(pid, api_headers)

            if eox_data:
                self.update_lifecycle_record(pid, hw_type, eox_data, config)
            else:
                self.log('ERROR', f'API Error: Failed to fetch data for {pid}')
