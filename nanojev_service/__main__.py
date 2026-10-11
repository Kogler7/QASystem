import logging
import os


def main():
    device = os.environ.get("NANOJEV_CUDA_VISIBLE_DEVICES", "").strip()
    if not device or "," in device:
        raise RuntimeError("NANOJEV_CUDA_VISIBLE_DEVICES must select one dedicated GPU")
    os.environ["CUDA_VISIBLE_DEVICES"] = device
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn
    uvicorn.run("nanojev_service.app:app", host=os.environ.get("NANOJEV_BIND", "127.0.0.1"),
                port=int(os.environ.get("NANOJEV_PORT", "8189")), workers=1, access_log=False)


if __name__ == "__main__":
    main()
