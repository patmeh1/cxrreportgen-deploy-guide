# Container image for the Service Bus worker that drives CxrReportGen.
# Build:  az acr build --registry <acr> --image cxr-worker:1.0 --file worker.Dockerfile .
# Deploy: az containerapp create ... --image <acr>.azurecr.io/cxr-worker:1.0 \
#                                    --min-replicas 0 --max-replicas 10 \
#                                    --scale-rule-name sb --scale-rule-type azure-servicebus ...
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    INPUT_CONTAINER=dicom-input \
    OUTPUT_CONTAINER=cxr-reports \
    SB_QUEUE=cxr-studies \
    SESSION_GATHER_SECONDS=30

RUN pip install \
        pydicom==2.4.* \
        pylibjpeg==2.0.* \
        pylibjpeg-libjpeg==2.0.* \
        pylibjpeg-openjpeg==2.0.* \
        Pillow==10.* \
        numpy==1.26.* \
        requests==2.32.* \
        azure-identity==1.* \
        azure-storage-blob==12.* \
        azure-servicebus==7.*

WORKDIR /app
COPY sb_worker.py /app/sb_worker.py

USER 1000

CMD ["python", "/app/sb_worker.py"]
