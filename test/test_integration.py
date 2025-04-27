import asyncio
import yaml
from garmin_gps_webapi import GarminGpsWebAPI

async def main():
    # Load configuration
    with open('resources/config.yml', 'r') as file:
        config = yaml.safe_load(file)

    gps_api = GarminGpsWebAPI(config)
    await gps_api.run()

if __name__ == '__main__':
    asyncio.run(main())