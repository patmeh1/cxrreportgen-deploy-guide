"""
Deploy Microsoft CxrReportGen from the AzureML system registry to a Managed Online Endpoint.

Prereqs:
  - Python 3.10
  - pip install azure-ai-ml azure-identity
  - Contributor or Owner on the target AML workspace
  - NCadsA100v4 quota >= 29 vCPU in the target region (24 + 20% upgrade reservation)

Source: https://github.com/Azure/azureml-examples/blob/main/sdk/python/foundation-models/healthcare-ai/cxrreportgen/cxr-deploy.ipynb
"""
import random
import string
from azure.ai.ml import MLClient
from azure.ai.ml.entities import (
    ManagedOnlineEndpoint,
    ManagedOnlineDeployment,
    OnlineRequestSettings,
)
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

# 1. Authenticate
try:
    credential = DefaultAzureCredential()
    credential.get_token("https://management.azure.com/.default")
except Exception:
    credential = InteractiveBrowserCredential()

# 2. Resolve the model from the AzureML system registry
registry_client = MLClient(credential, registry_name="azureml")
model = registry_client.models.get(name="CxrReportGen", label="latest")

# 3. Connect to your workspace (expects config.json or MLClient.from_config())
ml_client = MLClient.from_config(credential)

# 4. Create a uniquely-named endpoint
suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=5))
endpoint_name = f"cxrreportgen-{suffix}"
print(f"Endpoint: {endpoint_name}")

endpoint = ManagedOnlineEndpoint(name=endpoint_name, auth_mode="key")
ml_client.online_endpoints.begin_create_or_update(endpoint).result()

# 5. Create the deployment on an A100 80GB instance
deployment = ManagedOnlineDeployment(
    name="cxr-report-gen-v1",
    endpoint_name=endpoint_name,
    model=model,
    instance_type="Standard_NC24ads_A100_v4",
    instance_count=1,
    request_settings=OnlineRequestSettings(request_timeout_ms=90000),
    app_insights_enabled=True,
)
ml_client.online_deployments.begin_create_or_update(deployment).result()

# 6. Shift 100% of traffic to the new deployment
endpoint.traffic = {"cxr-report-gen-v1": 100}
ml_client.online_endpoints.begin_create_or_update(endpoint).result()

print(f"Deployed. Scoring URI: {ml_client.online_endpoints.get(endpoint_name).scoring_uri}")
