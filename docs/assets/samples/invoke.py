"""Invoke a deployed CxrReportGen endpoint with frontal + lateral chest X-rays."""
import base64
import json
from azure.ai.ml import MLClient
from azure.identity import DefaultAzureCredential

ENDPOINT_NAME = "cxrreportgen-xxxxx"   # set yours
DEPLOYMENT_NAME = "cxr-report-gen-v1"

ml_client = MLClient.from_config(DefaultAzureCredential())


def read_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.encodebytes(f.read()).decode("utf-8")


payload = {
    "input_data": {
        "columns": ["frontal_image", "lateral_image", "indication", "technique", "comparison"],
        "index": [0],
        "data": [[
            read_b64("./images/cxr_frontal.jpg"),
            read_b64("./images/cxr_lateral.jpg"),
            "65 y/o M with cough and SOB",
            "PA and lateral chest radiograph",
            "None",
        ]],
    },
    "params": {},
}

with open("request.json", "w") as f:
    json.dump(payload, f)

response = ml_client.online_endpoints.invoke(
    endpoint_name=ENDPOINT_NAME,
    deployment_name=DEPLOYMENT_NAME,
    request_file="request.json",
)
findings = json.loads(json.loads(response)[0]["output"])
for idx, (text, boxes) in enumerate(findings):
    print(f"{idx}. {text}  bboxes={boxes}")
