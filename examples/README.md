# Examples

Reference tools for live panel testing. Requires **Python 3.10+**. No third-party dependencies.

## Environment variables

Both scripts accept the same common variables:

| Variable | Used by | Description |
|----------|---------|-------------|
| `INIM_HOST` | client, proxy | Panel IP or hostname |
| `INIM_PORT` | client, proxy | Panel TCP port (default `5004`) |
| `INIM_PIN` | client | User PIN for `arm` / `disarm` |
| `INIM_AREAS` | client | Number of areas (default `5`) |
| `INIM_PROXY_LISTEN` | proxy | Local bind address (default `0.0.0.0`) |
| `INIM_PROXY_PORT` | proxy | Local listen port (default `5004`) |
| `INIM_PROXY_LOG_DIR` | proxy | Log directory (default `capture_logs`) |

## `inim_client.py`

CLI client implementing the protocol documented in the root README.

```bash
export INIM_HOST=192.168.1.50
export INIM_PIN=1234

python inim_client.py status
python inim_client.py version
python inim_client.py --areas 5 zones --zones 20
python inim_client.py scenarios --stride 5 --addr 0x142BF
python inim_client.py arm --mode away --area 1 --code $INIM_PIN --yes
python inim_client.py disarm --area 1 --code $INIM_PIN --yes
```

`--host` is required if `INIM_HOST` is not set. `--code` (or `INIM_PIN`) is required for arm/disarm.

## `alarm_proxy.py`

Transparent TCP proxy for traffic capture. Listens locally and forwards to the panel.

```bash
python alarm_proxy.py --target 192.168.1.50
python alarm_proxy.py --target panel.local --listen-port 5004 --log-dir capture_logs
```

Point SmartLeague at the proxy machine (same port as `--listen-port`, default 5004).

Press **Space** during capture to insert START/END log markers around manual operations.
