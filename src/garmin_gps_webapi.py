"""
garmin_gps_webapi.py
--------------------
Thin async client for the X-Plane 12 Web API focused on Garmin-GPS datarefs.

Main features
=============
* Fetches the numeric IDs for a curated set of datarefs (see ``DATAREF_NAMES``)
  so that subscription works across simulator sessions.
* Opens a Web-socket connection, subscribes to those IDs, and provides:
  - ``__aiter__``      → raw parsed events
  - ``stream_updates`` → same, plus::

        PauseState(paused=True)   # when traffic stops for *pause_timeout*
        PauseState(paused=False)  # first frame after silence
        Disconnected(reason=str)  # Web-socket dropped (sim closed)

* Typed events are declared via :py:data:`dataclasses.dataclass` for ergonomic
  ``match``/``isinstance`` handling.

Typical usage
-------------
```python
from garmin_gps_webapi import GarminGpsWebAPI, FlightLoopUpdate

cfg = yaml.safe_load(open("resources/config.yml"))
api = GarminGpsWebAPI(cfg)

await api.fetch_dataref_ids()
await api.connect_websocket()

async for ev in api.stream_updates(pause_timeout=1.5):
    if isinstance(ev, FlightLoopUpdate):
        print(ev.values)
```

Configuration keys referenced
-----------------------------
```
x-plane:
  connection:
    webapi:
      url            : "http://localhost:8086/api/v2"
```

The connector module wraps this client and converts frames to Garmin
“Aviation-n” serial sentences.
"""
import asyncio
import json
import logging
import aiohttp
import websockets
import yaml
from dataclasses import dataclass

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load configuration
def load_config():
    with open('resources/config.yml', 'r') as file:
        return yaml.safe_load(file)

# Define the datarefs you want to subscribe to
DATAREF_NAMES = [
    "sim/flightmodel/position/latitude",
    "sim/flightmodel/position/longitude",
    "sim/flightmodel/position/elevation",
    "sim/flightmodel/position/mag_psi",
    "sim/flightmodel/position/magnetic_variation",
    "sim/cockpit2/gauges/indicators/airspeed_kts_pilot",
    "sim/time/paused"
]

# ---------------------------------------------------------------------#
# Typed events returned by the async iterator                          #
# ---------------------------------------------------------------------#
@dataclass
class FlightLoopUpdate:
    """High-rate update with many datarefs (type=dataref_update_values)."""
    values: dict   # {dataref_id: value}

@dataclass
class ErrorResult:
    """Error frame coming back from the Web API (type=result, success=False)."""
    req_id: int
    error_code: str
    error_message: str

@dataclass
class ResultOK:
    """Ack frame (type=result, success=True)."""
    req_id: int

@dataclass
class PauseState:
    """Synthetic event: emitted when traffic stops/resumes."""
    paused: bool  # True = assumed paused, False = traffic resumed

@dataclass
class Disconnected:
    """Emitted when the WebSocket drops (e.g. X-Plane quits)."""
    reason: str

@dataclass
class MiscEvent:
    """Any other JSON frame we don't specially handle."""
    payload: dict

class GarminGpsWebAPI:
    """
    Thin typed wrapper around the X-Plane 12 Web API focused on a handful of
    Garmin-relevant datarefs.

    Public workflow
    ---------------
    1. **`fetch_dataref_ids()`** - Look up the numeric IDs that X-Plane assigns
       to each dataref name.  Must be called once per simulator session.
    2. **`connect_websocket()`** - Open the Web-socket and subscribe to those
       IDs so X-Plane starts pushing updates.
    3. **`stream_updates()`** - Async generator that yields:
         * :class:`FlightLoopUpdate`   - every *dataref_update_values* frame  
         * :class:`PauseState`         - synthetic pause / resume signals  
         * :class:`Disconnected`       - Web-socket unexpectedly closed  
         * :class:`ErrorResult` / :class:`ResultOK` for *result* frames  
       This is what the connector module consumes.
    4. The class itself is an async iterator (``__aiter__``) if you only want
       raw event frames without pause detection.

    Notes
    -----
    * Pause detection uses a watchdog: if no frame arrives for
      ``pause_timeout`` seconds, a *PauseState(paused=True)* is emitted.
    * All events are small :py:data:`dataclasses.dataclass` instances, making
      them friendly to ``match`` or ``isinstance`` dispatch.
    * The reverse mapping ``id_to_name`` is populated so higher-level code can
      translate the numeric IDs back to human-readable names if required.
    """
    def __init__(self, config):
        self.config = config
        self.dataref_ids = {}
        self.websocket = None
        self.next_req_id = 1
        self.id_to_name = {}  # maps dataref ID to its name

    async def fetch_dataref_ids(self):
        """
        Use the Web API's filter endpoint to retrieve IDs for specific datarefs.
        (One request per dataref, because batch filtering is not supported.)
        """
        base_url = self.config['x-plane']['connection']['webapi']['url']

        async with aiohttp.ClientSession() as session:
            for name in DATAREF_NAMES:
                params = {
                    'filter[name]': name,
                    'fields': 'id,name'
                }
                async with session.get(f"{base_url}/datarefs", params=params) as resp:
                    if resp.status == 404:
                        logger.warning(f"Dataref {name} not found.")
                        continue
                    if resp.status != 200:
                        logger.error(f"Failed to fetch dataref {name}: {resp.status}")
                        continue
                    data = await resp.json()
                    for item in data['data']:
                        self.dataref_ids[item['name']] = item['id']
                        self.id_to_name[item['id']] = item['name']  # <-- store reverse mapping
                        logger.info(f"Retrieved ID {item['id']} for {item['name']}")

        logger.info(f"Final dataref IDs: {self.dataref_ids}")

    async def connect_websocket(self):
        """
        Connect to the WebSocket and subscribe to the desired datarefs.
        """
        ws_url = self.config['x-plane']['connection']['webapi']['url'].replace('http', 'ws')
        self.websocket = await websockets.connect(ws_url)

        # Generate unique request id
        req_id = self.next_req_id
        self.next_req_id += 1

        # Build the subscription payload
        payload = {
            "req_id": req_id,
            "type": "dataref_subscribe_values",
            "params": {
                "datarefs": [{"id": dataref_id} for dataref_id in self.dataref_ids.values()]
            }
        }
        message = json.dumps(payload)
        logger.info(f"Subscription Message: {message}")
        await self.websocket.send(message)
        logger.info(f"Subscribed to {len(self.dataref_ids)} datarefs with req_id={req_id}")

    # ------------------------------------------------------------------ #
    # Allow:  async for msg in gps_webapi                                 #
    # ------------------------------------------------------------------ #
    async def __aiter__(self):
        """
        Async iterator that yields each incoming Web-socket frame
        parsed as JSON.  Call connect_websocket() first.
        """
        if self.websocket is None:
            raise RuntimeError("WebSocket not connected; call connect_websocket() first.")

        while True:
            raw = await self.websocket.recv()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON frame: %s", raw)
                continue

            yield self._parse_event(data)

    # ------------------------------------------------------------------ #
    # Translate a Web-socket JSON frame into a typed event               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_event(data: dict):
        msg_type = data.get("type")
        if msg_type == "dataref_update_values":
            return FlightLoopUpdate(values=data.get("data", {}))

        if msg_type == "result":
            if data.get("success", True):
                return ResultOK(req_id=data.get("req_id"))
            return ErrorResult(
                req_id=data.get("req_id"),
                error_code=data.get("error_code"),
                error_message=data.get("error_message"),
            )

        # Fallback: wrap raw payload
        return MiscEvent(payload=data)

    # ------------------------------------------------------------------ #
    # High-level stream that auto-detects pauses via watchdog            #
    # ------------------------------------------------------------------ #
    async def stream_updates(self, pause_timeout: float = 1.5):
        """
        Async generator that yields parsed events (like __aiter__) **plus**
        PauseState(paused=True/False) when no traffic is seen for
        `pause_timeout` seconds.

        Usage:
        >>> async for ev in api.stream_updates(1.5):
        ...     ...
        """
        if self.websocket is None:
            raise RuntimeError("WebSocket not connected; call connect_websocket() first.")

        paused = False
        while True:
            try:
                try:
                    raw = await asyncio.wait_for(self.websocket.recv(), timeout=pause_timeout)
                except websockets.exceptions.ConnectionClosed as exc:
                    logger.warning("WebSocket closed: %s", exc)
                    yield Disconnected(reason=str(exc))
                    break  # exit generator
            except asyncio.TimeoutError:
                if not paused:
                    paused = True
                    yield PauseState(paused=True)
                continue  # go back to waiting for traffic

            # Got traffic → parse
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON frame: %s", raw)
                continue

            event = self._parse_event(data)

            # If we were paused, emit resume
            if paused:
                paused = False
                yield PauseState(paused=False)

            yield event