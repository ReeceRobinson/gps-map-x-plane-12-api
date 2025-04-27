import asyncio
import json
import logging
import time
import aiohttp
import websockets
import yaml

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

class GarminGpsWebAPI:
    def __init__(self, config):
        self.config = config
        self.dataref_ids = {}
        self.websocket = None
        self.next_req_id = 1
        self.id_to_name = {}  # maps dataref ID to its name
        self.last_dataref_values = {}
        self.last_dataref_times = {}
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

                    for dataref_id_str, value in values.items():
                        dataref_id = int(dataref_id_str)
                        name = self.id_to_name.get(dataref_id, f"Unknown ID {dataref_id}")

                        last_value = self.last_dataref_values.get(dataref_id)
                        value_changed = last_value is None or abs(value - last_value) > self.value_threshold

                        if value_changed: #and time_passed:
                            self.handle_dataref_update(name, value)
                            self.last_dataref_values[dataref_id] = value

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

    # ------------------------------------------------------------------ #
    # Helper: build one Aviation‑In sentence                              #
    # ------------------------------------------------------------------ #
    def build_sentence(self, latitude, longitude, elevation_m,
                       mag_psi, magnetic_variation_deg, knots):
        """
        Format a complete Aviation‑In ASCII sentence.
        """
        altitude_ft = int(elevation_m * 3.28084)

        lat_h, lat_deg, lat_min = self.convert_lat(latitude)
        lon_h, lon_deg, lon_min = self.convert_lon(longitude)

        compass = int(round(mag_psi))
        mag_var_tenths = int(round(magnetic_variation_deg * 10))
        mag_prefix = "W" if mag_var_tenths >= 0 else "E"

        return (
            "z{alt:05d}\r\n"
            "A{lat_h} {lat_deg} {lat_min}\r\n"
            "B{lon_h} {lon_deg} {lon_min}\r\n"
            "C{comp:03d}\r\n"
            "D{spd:03d}\r\n"
            "E00000\r\n"
            "GR0000\r\n"
            "I0000\r\n"
            "KL0000\r\n"
            "Q{mag_pref}{mag:03d}\r\n"
            "S-----\r\n"
            "T---------\r\n"
            "w01@\r\n"
        ).format(
            alt=altitude_ft,
            lat_h=lat_h, lat_deg=lat_deg, lat_min=lat_min,
            lon_h=lon_h, lon_deg=lon_deg, lon_min=lon_min,
            comp=compass,
            spd=knots,
            mag_pref=mag_prefix,
            mag=abs(mag_var_tenths)
        )

    async def run(self):
        await self.fetch_dataref_ids()
        await self.connect_websocket()
        await self.receive_data()

if __name__ == "__main__":

    config = load_config()
    gps_webapi = GarminGpsWebAPI(config)
    asyncio.run(gps_webapi.run())