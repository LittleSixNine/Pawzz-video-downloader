"""Resolve a feeder MAC on the NAS's directly connected IPv4 network."""
import ipaddress
import json
import re
import socket
import subprocess
import time


class DiscoveryError(RuntimeError):
    pass


def normalize_mac(value):
    value = value.strip().lower().replace('-', ':')
    if (not re.fullmatch(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}', value)
            or int(value[:2], 16) & 1 or value == '00:00:00:00:00:00'):
        raise ValueError('invalid device MAC')
    return value


def ip_rows(*arguments):
    try:
        result = subprocess.run(['ip', '-j', '-4', *arguments], check=True,
                                capture_output=True, text=True, timeout=3)
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError()
        return rows
    except (OSError, ValueError, subprocess.SubprocessError):
        raise DiscoveryError('device_discovery_unavailable') from None


def discover_device(mac, callback_ip):
    mac = normalize_mac(mac)
    local = ipaddress.IPv4Address(callback_ip)
    try:
        interfaces = {(row['ifname'], info['prefixlen'])
                      for row in ip_rows('address', 'show')
                      for info in row.get('addr_info', []) if info.get('local') == str(local)}
        if len(interfaces) != 1:
            raise DiscoveryError('device_network_unsupported')
        interface, prefix = interfaces.pop()
        network = ipaddress.IPv4Network((str(local), prefix), strict=False)
        # shortcut: bound discovery to 1024 addresses; use DHCP reservation on larger LANs.
        if network.num_addresses > 1024:
            raise DiscoveryError('device_network_unsupported')
        # UDP lets the kernel perform ARP without root or raw-socket capabilities.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind((str(local), 0))
            probe.setblocking(False)
            for address in network.hosts():
                if address != local:
                    try:
                        probe.sendto(b'\0', (str(address), 9))
                    except OSError:
                        pass
                    time.sleep(.01)
        # Allow STALE -> DELAY -> PROBE to finish; never trust a stale MAC mapping.
        time.sleep(10)
        found = set()
        for row in ip_rows('neigh', 'show', 'dev', interface):
            if row.get('lladdr', '').lower() != mac or 'REACHABLE' not in row.get('state', []):
                continue
            address = ipaddress.IPv4Address(row['dst'])
            if address in network and address not in (local, network.network_address, network.broadcast_address):
                found.add(str(address))
    except (OSError, ValueError, KeyError, TypeError):
        raise DiscoveryError('device_discovery_unavailable') from None
    if not found:
        raise DiscoveryError('device_not_found')
    if len(found) != 1:
        raise DiscoveryError('device_ambiguous')
    return found.pop()
