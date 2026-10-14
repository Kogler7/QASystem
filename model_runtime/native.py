"""Native model process initialization shared by Clef and NanoJev."""
import os
from pathlib import Path
from qasystem_config.model_registry import selected_native, validate_record, physical_gpu, select_native_record

def native_main(adapter):
    import argparse
    import logging
    parser = argparse.ArgumentParser()
    parser.add_argument('--record', required=True)
    parser.add_argument('--container', action='store_true')
    args = parser.parse_args()
    select_native_record(args.record)
    record = selected_native(adapter)
    validate_record(args.record, record, root=Path('/models'), check_files=True)
    if record.get('manager', 'ops') != 'ops': raise ValueError('模型不是 ops 独立服务')
    os.environ['CUDA_DEVICE_ORDER'] = 'PCI_BUS_ID'
    os.environ['CUDA_VISIBLE_DEVICES'] = physical_gpu(record)
    if physical_gpu(record):
        import torch
        if not torch.cuda.is_available(): raise RuntimeError('指定 GPU 不可用，不会回退到 CPU')
        torch.cuda.set_per_process_memory_fraction(float(record['utilization']), 0)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    import uvicorn
    uvicorn.run(adapter + '_service.app:app', host='0.0.0.0' if args.container else record['bind'],
                port=8000 if args.container else int(record['port']), workers=1, access_log=False)

