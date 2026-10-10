"""Start exactly one model process with ``python -m clef_service``."""

import argparse
import logging
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Clef-flash HTTP service")
    parser.add_argument("--host", default=os.environ.get("CLEF_BIND", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLEF_PORT", "8188")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if os.environ.get("CLEF_CUDA_VISIBLE_DEVICES"):
        os.environ["CUDA_VISIBLE_DEVICES"] = os.environ["CLEF_CUDA_VISIBLE_DEVICES"]
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    import uvicorn

    uvicorn.run("clef_service.app:app", host=args.host, port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
