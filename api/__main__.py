"""Start the LANIS API with the values from config.json."""

import uvicorn

from .server_config import load_server_config


def main() -> None:
    config = load_server_config()
    uvicorn.run("api.api:app", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
