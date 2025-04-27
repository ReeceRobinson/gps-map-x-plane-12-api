import asyncio
import json
import logging
import sys
import time
import aiohttp
import websockets
import yaml

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GarminGpsWebAPI")

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

class GarminGpsWebAPI:
    def __init__(self, config):
        self.config = config
        self.dataref_ids = {}
        self.websocket = None
        self.next_req_id = 1
        self.id_to_name = {}  # maps dataref ID to its name
        self.last_dataref_values = {}
        self.last_dataref_times = {}
        self.log_interval = 2.0  # seconds
        self.value_threshold = 0.001
        self.last_processed_time = 0
        self.processing_interval = 1  # seconds (equivalent to 1Hz)

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

    async def receive_data(self):
        """
        Receive data from the WebSocket and process it.
        """
        try:
            async for message in self.websocket:
                current_time = time.time()
                if current_time - self.last_processed_time < self.processing_interval:
                    continue  # Skip processing to throttle the rate
                self.last_processed_time = current_time
            
                data = json.loads(message)
                message_type = data.get("type")

                if message_type == "dataref_update_values":
                    values = data.get("data", {})

                    now = time.time()
                    for dataref_id_str, value in values.items():
                        dataref_id = int(dataref_id_str)
                        name = self.id_to_name.get(dataref_id, f"Unknown ID {dataref_id}")

                        last_value = self.last_dataref_values.get(dataref_id)
                        # last_time = self.last_dataref_times.get(dataref_id, 0)

                        value_changed = last_value is None or abs(value - last_value) > self.value_threshold
                        # time_passed = (now - last_time) > self.log_interval

                        if value_changed: #and time_passed:
                            self.handle_dataref_update(name, value)
                            self.last_dataref_values[dataref_id] = value
                            self.last_dataref_times[dataref_id] = now

                elif message_type == "dataref_values":
                    params = data.get("params", {})
                    datarefs = params.get("datarefs", [])
                    for entry in datarefs:
                        logger.info(f"Dataref ID {entry['id']} = {entry['value']}")
                else:
                    logger.debug(f"Unknown message type: {message_type}")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed.")

    def handle_dataref_update(self, name: str, value: float):
        """
        Handle updated dataref values.
        For now, just log them. Later, you can send to serial, file, etc.
        """
        logger.info(f"Update -> {name}: {value:.6f}")

    async def run(self):
        await self.fetch_dataref_ids()
        await self.connect_websocket()
        await self.receive_data()

if __name__ == "__main__":
    config = load_config()
    gps_webapi = GarminGpsWebAPI(config)
    asyncio.run(gps_webapi.run())