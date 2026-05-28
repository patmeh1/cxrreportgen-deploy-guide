# Container image for the DIMSE C-STORE SCP listener.
# Build:  az acr build --registry <acr> --image cxr-dimse-listener:1.0 --file listener.Dockerfile .
# Deploy: az containerapp create ... --image <acr>.azurecr.io/cxr-dimse-listener:1.0 \
#                                    --ingress internal --transport tcp --target-port 11112 ...
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AE_TITLE=CXR-INTAKE \
    LISTEN_PORT=11112 \
    INPUT_CONTAINER=dicom-input \
    SB_QUEUE=cxr-studies

RUN pip install \
        pynetdicom==2.1.* \
        pydicom==2.4.* \
        azure-identity==1.* \
        azure-storage-blob==12.* \
        azure-servicebus==7.*

WORKDIR /app
COPY dimse_listener.py /app/dimse_listener.py

EXPOSE 11112
USER 1000

CMD ["python", "/app/dimse_listener.py"]
