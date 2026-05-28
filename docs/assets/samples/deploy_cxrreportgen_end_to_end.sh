#!/usr/bin/env bash
# ============================================================================
#  deploy_cxrreportgen_end_to_end.sh
#  ---------------------------------------------------------------------------
#  Empty Azure subscription -> finished radiology report in Blob Storage.
#  Stands up Microsoft Foundry hub + project, dedicated locked-down storage,
#  RBAC, the CxrReportGen managed online endpoint (A100), and runs the
#  full inference pipeline end-to-end. Single file, no companions.
#
#  Usage:    bash deploy_cxrreportgen_end_to_end.sh
#  Override: SUB=... LOC=... RG=... DICOM_FRONTAL=./mine.dcm bash ...
#  Cleanup:  az group delete -n "$RG" -y --no-wait
#
#  Expected cost: ~$8-10 if you delete the endpoint immediately after the
#  first successful inference. The A100 VM is the only meaningful meter
#  (~$3.67/hour list); storage + KV + App Insights add up to pennies.
# ============================================================================

set -euo pipefail

# ─── inputs (env var overrides supported) ──────────────────────────────────
SUB="${SUB:-$(az account show --query id -o tsv 2>/dev/null || true)}"
LOC="${LOC:-eastus}"
RG="${RG:-cxr-rg}"
HUB="${HUB:-cxr-foundry-hub}"
PROJ="${PROJ:-cxr-foundry-proj}"
SA="${SA:-cxrdicom$RANDOM}"
IN_CONTAINER="${IN_CONTAINER:-dicom-input}"
OUT_CONTAINER="${OUT_CONTAINER:-cxr-reports}"
EP="${EP:-cxr-endpoint-001}"
DEP="${DEP:-cxr-v1}"
SKU="${SKU:-Standard_NC24ads_A100_v4}"
STUDY_ID="${STUDY_ID:-study-001}"
DICOM_FRONTAL="${DICOM_FRONTAL:-}"
DICOM_LATERAL="${DICOM_LATERAL:-}"
WORKDIR="${WORKDIR:-$(pwd)/cxr-work}"
mkdir -p "$WORKDIR"
export SUB LOC RG HUB PROJ SA IN_CONTAINER OUT_CONTAINER EP DEP SKU STUDY_ID DICOM_FRONTAL DICOM_LATERAL WORKDIR

# ─── pretty printing ───────────────────────────────────────────────────────
TOTAL=22
__b=$'\033[1m'; __d=$'\033[2m'; __c=$'\033[36m'; __g=$'\033[32m'
__y=$'\033[33m'; __r=$'\033[31m'; __0=$'\033[0m'
step() {
  local n="$1"; shift
  printf '\n%s%s════════════════════════════════════════════════════════════%s\n' "$__c" "$__b" "$__0"
  printf '%s%s STEP %2d/%d  %s%s\n' "$__c" "$__b" "$n" "$TOTAL" "$*" "$__0"
  printf '%s%s════════════════════════════════════════════════════════════%s\n' "$__c" "$__b" "$__0"
}
note() { printf '  %s· %s%s\n' "$__d" "$*" "$__0"; }
ok()   { printf '  %s✓ %s%s\n' "$__g" "$*" "$__0"; }
warn() { printf '  %s! %s%s\n' "$__y" "$*" "$__0"; }
die()  { printf '  %s✗ %s%s\n' "$__r" "$*" "$__0"; exit 1; }

# ============================================================================
step 1 "Pre-flight — verify CLI tools"
# ============================================================================
note "Need: az (>=2.50), python3 (>=3.10), jq, curl. We will (re)install the"
note "azure-cli 'ml' extension which provides Foundry hub/project commands."
for c in az python3 jq curl; do command -v "$c" >/dev/null || die "missing dependency: $c"; done
ok "az      -> $(az version --query '\"azure-cli\"' -o tsv 2>/dev/null)"
ok "python3 -> $(python3 -V 2>&1)"
ok "jq      -> $(jq --version)"
az extension add -n ml --upgrade --only-show-errors >/dev/null
ok "az ml   -> $(az extension show -n ml --query version -o tsv)"

# ============================================================================
step 2 "Verify Azure sign-in + subscription"
# ============================================================================
[ -n "$SUB" ] || die "Not logged in. Run: az login"
note "Setting active subscription so every subsequent az command targets the"
note "same place; az login can attach multiple subs and the active one is sticky."
az account set --subscription "$SUB"
ok "Subscription: $(az account show --query '[name, id]' -o tsv | xargs)"
ok "Tenant:       $(az account show --query tenantId -o tsv)"
ok "Signed-in:    $(az account show --query user.name -o tsv)"

# ============================================================================
step 3 "Print effective configuration (sanity check)"
# ============================================================================
cat <<EOF
  ┌─────────────────────────────────────────────────────────────────┐
  │ region          : $LOC
  │ resource group  : $RG
  │ Foundry hub     : $HUB
  │ Foundry project : $PROJ
  │ storage account : $SA
  │   in container   : $IN_CONTAINER
  │   out container  : $OUT_CONTAINER
  │ endpoint name   : $EP        deployment: $DEP
  │ GPU SKU         : $SKU  (~\$3.67/hour list)
  │ study id        : $STUDY_ID
  │ working dir     : $WORKDIR
  └─────────────────────────────────────────────────────────────────┘
EOF
note "Wrong? Ctrl-C now, re-export the env vars, and re-run."
sleep 2

# ============================================================================
step 4 "Register required Azure Resource Providers"
# ============================================================================
note "On a brand new subscription, these are NOT all registered by default."
note "Skipping this step would let later 'az' commands fail with the very"
note "confusing 'subscription is not registered to use namespace ...' error."
for ns in Microsoft.MachineLearningServices Microsoft.Storage Microsoft.KeyVault \
          Microsoft.ContainerRegistry Microsoft.Insights Microsoft.OperationalInsights \
          Microsoft.CognitiveServices Microsoft.Authorization Microsoft.Network; do
  state=$(az provider show -n "$ns" --query registrationState -o tsv 2>/dev/null || echo "NotRegistered")
  if [ "$state" != "Registered" ]; then
    note "registering $ns (was $state) ..."
    az provider register -n "$ns" --wait --only-show-errors
  fi
  ok "$ns: $(az provider show -n "$ns" --query registrationState -o tsv)"
done

# ============================================================================
step 5 "Create / reuse the resource group"
# ============================================================================
note "Cost meter starts here. Everything we create lives inside this RG so the"
note "tear-down is one command at the end (az group delete -n \$RG -y --no-wait)."
if az group show -n "$RG" >/dev/null 2>&1; then
  warn "RG '$RG' already exists in $(az group show -n "$RG" --query location -o tsv) — reusing."
else
  az group create -n "$RG" -l "$LOC" --only-show-errors >/dev/null
fi
ok "Resource group ready: $RG ($LOC)"

# ============================================================================
step 6 "Quota check — NCadsA100v4 family"
# ============================================================================
note "CxrReportGen ships a container that serves on a single NVIDIA A100 80GB."
note "Region quota lives under 'Standard NCADSA100v4 Family vCPUs' and is *zero*"
note "on every new subscription. The deployment in Step 16 will hard-fail if the"
note "family quota is below 24 vCPUs."
USAGE=$(az ml compute list-usage --location "$LOC" \
  --query "[?contains(name.value,'NCADSA100v4')].{n:name.localizedValue,used:currentValue,lim:limit}" -o json 2>/dev/null || echo "[]")
echo "$USAGE" | jq -r '.[] | "  · \(.n): used \(.used) / quota \(.lim)"' || true
LIMIT=$(echo "$USAGE" | jq -r '[.[].lim] | first // 0')
if [ "${LIMIT:-0}" -lt 24 ]; then
  warn "A100 v4 family quota looks insufficient (need >=24 vCPUs)."
  warn "Request a bump: Azure Portal -> Subscriptions -> Usage + quotas, or"
  warn "  az support tickets create ... (see Prerequisites tab on the guide site)."
  warn "Continuing anyway — Step 16 will fail loudly if quota is the blocker."
else
  ok "A100 v4 family quota: $LIMIT vCPUs (>=24 required)"
fi

# ============================================================================
step 7 "Create Microsoft Foundry HUB"
# ============================================================================
note "Foundry topology:"
note "  hub      = shared infra (storage, KV, App Insights, Container Registry,"
note "             connections, RBAC root). One hub per team is typical."
note "  project  = where your endpoint actually lives. CxrReportGen deployment"
note "             attaches to the project."
note ""
note "'--kind hub' auto-provisions a workspace storage account, Key Vault, and"
note "App Insights in this RG. First create takes 3-5 min."
if az ml workspace show -g "$RG" -n "$HUB" >/dev/null 2>&1; then
  warn "Hub '$HUB' already exists — reusing."
else
  az ml workspace create --kind hub -g "$RG" -l "$LOC" -n "$HUB" --only-show-errors >/dev/null
fi
HUB_ID=$(az ml workspace show -g "$RG" -n "$HUB" --query id -o tsv)
ok "Foundry hub:     $HUB"
ok "Hub resource id: $HUB_ID"
note "Auto-provisioned dependencies now live in $RG:"
az resource list -g "$RG" --query "[].{name:name, type:type}" -o table 2>/dev/null | head -20 || true

# ============================================================================
step 8 "Create Foundry PROJECT under the hub + write config.json"
# ============================================================================
note "A Foundry project IS an Azure ML workspace at the API level — same SDK,"
note "same 'az ml' commands. The '--hub-id' link is what makes ai.azure.com"
note "render it under the hub. config.json points downstream Python at the"
note "PROJECT (not the hub), so MLClient.from_config() works without args."
if az ml workspace show -g "$RG" -n "$PROJ" >/dev/null 2>&1; then
  warn "Project '$PROJ' already exists — reusing."
else
  az ml workspace create --kind project -g "$RG" -l "$LOC" -n "$PROJ" \
      --hub-id "$HUB_ID" --only-show-errors >/dev/null
fi
ok "Foundry project: $PROJ"
cat > "$WORKDIR/config.json" <<EOF
{
  "subscription_id": "$SUB",
  "resource_group": "$RG",
  "workspace_name": "$PROJ"
}
EOF
ok "config.json     -> $WORKDIR/config.json"

# ============================================================================
step 9 "Create dedicated Blob Storage account for the DICOM pipeline"
# ============================================================================
note "Why a NEW account instead of reusing the hub's auto-created storage?"
note "  - keeps PHI-bearing DICOMs separate from model artifacts (audit / IAM)"
note "  - lets you tune lifecycle rules, CMK, and soft-delete for clinical data"
note "  - simpler to disable / wipe at end-of-study"
note ""
note "Account hardening flags we set on creation:"
note "  --sku Standard_LRS              cheapest tier; switch to ZRS for HA"
note "  --kind StorageV2                only kind supporting blob lifecycle + CMK"
note "  --min-tls-version TLS1_2        rejects legacy clients"
note "  --allow-blob-public-access false  no anonymous URLs, ever"
note "  --https-only true               rejects http://"
note ""
note "Networking: account is public-endpoint by default. For locked-down deploys"
note "switch to a Private Endpoint + service tag 'MachineLearningServices' on"
note "the endpoint's VNet (see Networking & Security tab on the guide site)."
if az storage account show -g "$RG" -n "$SA" >/dev/null 2>&1; then
  warn "Storage account '$SA' already exists — reusing."
else
  az storage account create -g "$RG" -l "$LOC" -n "$SA" \
      --sku Standard_LRS --kind StorageV2 \
      --min-tls-version TLS1_2 \
      --allow-blob-public-access false \
      --https-only true --only-show-errors >/dev/null
fi
ok "Storage account: $SA  (sku=$(az storage account show -g "$RG" -n "$SA" --query sku.name -o tsv))"

# ============================================================================
step 10 "Create input + output blob containers"
# ============================================================================
note "Container layout we will use:"
note "  ${IN_CONTAINER}/<study-id>/{frontal,lateral}.dcm   <- DICOMs in"
note "  ${OUT_CONTAINER}/<study-id>/{report.json,report.md,frontal_overlay.png}"
SA_KEY=$(az storage account keys list -g "$RG" -n "$SA" --query "[0].value" -o tsv)
for c in "$IN_CONTAINER" "$OUT_CONTAINER"; do
  az storage container create -n "$c" --account-name "$SA" --account-key "$SA_KEY" \
     --only-show-errors >/dev/null
  ok "container ready: $c"
done

# ============================================================================
step 11 "Grant 'Storage Blob Data Contributor' RBAC (no keys in code)"
# ============================================================================
note "Best practice: never bake the account key into inference code. The Foundry"
note "project already has a system-assigned managed identity (MI). We grant it"
note "Storage Blob Data Contributor on the storage account so DefaultAzureCredential"
note "inside the endpoint code can read DICOMs and write reports with no secret"
note "in source. We also self-grant so the local Python here can do the same."
MI=$(az ml workspace show -g "$RG" -n "$PROJ" --query identity.principal_id -o tsv)
SA_ID=$(az storage account show -g "$RG" -n "$SA" --query id -o tsv)
if az role assignment list --assignee "$MI" --scope "$SA_ID" --role "Storage Blob Data Contributor" -o tsv 2>/dev/null | grep -q .; then
  ok "role assignment for project MI already in place"
else
  az role assignment create --assignee "$MI" --role "Storage Blob Data Contributor" \
     --scope "$SA_ID" --only-show-errors >/dev/null
  ok "Storage Blob Data Contributor granted to project MI ($MI)"
fi
ME=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
if [ -n "$ME" ]; then
  az role assignment create --assignee "$ME" --role "Storage Blob Data Contributor" \
     --scope "$SA_ID" --only-show-errors >/dev/null 2>&1 || true
  ok "self-RBAC granted to $(az ad signed-in-user show --query userPrincipalName -o tsv)"
fi
note "RBAC propagation can take 30-60 seconds — sleeping to avoid 403s on the"
note "first blob upload below."
sleep 45

# ============================================================================
step 12 "Create Foundry Connection that wraps the storage account"
# ============================================================================
note "Foundry Connections are the recommended way to point a project at outside"
note "resources (blob, OpenAI, Key Vault, search, ...). They show up in the"
note "Foundry portal under Project -> Connected resources and can be selected"
note "from notebooks via the azure-ai-projects SDK. Using managed_identity here"
note "means the project's MI (the one we just RBAC'd) is what authenticates."
if az ml connection show -g "$RG" --workspace-name "$PROJ" -n cxr-dicom-storage >/dev/null 2>&1; then
  warn "Connection 'cxr-dicom-storage' already exists — reusing."
else
  az ml connection create --type azure_blob -g "$RG" --workspace-name "$PROJ" \
     -n cxr-dicom-storage \
     --target "https://${SA}.blob.core.windows.net/${IN_CONTAINER}" \
     --credentials type=managed_identity --only-show-errors >/dev/null
fi
ok "Foundry Connection: cxr-dicom-storage -> https://${SA}.blob.core.windows.net/${IN_CONTAINER}"

# ============================================================================
step 13 "Create Python venv + install client packages"
# ============================================================================
note "Pin to a venv so we don't pollute the system interpreter. The set:"
note "  azure-identity        DefaultAzureCredential / AzureCliCredential"
note "  azure-ai-ml           managed online endpoints + model resolution"
note "  azure-ai-projects     Foundry-native helpers"
note "  azure-storage-blob    blob I/O for DICOM in / report out"
note "  healthcareai_toolkit  CxrReportGenClient + DICOM windowing"
note "  pydicom, SimpleITK    DICOM read/write"
note "  numpy, pillow         overlay rendering"
VENV="$WORKDIR/.venv"
[ -d "$VENV" ] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
python -m pip install --upgrade pip --quiet
pip install --quiet \
  "azure-identity>=1.15" "azure-ai-ml>=1.18" "azure-ai-projects>=1.0.0b6" \
  "azure-storage-blob>=12.20" "numpy" "pillow" "pydicom>=2.4" "SimpleITK>=2.3" \
  "git+https://github.com/microsoft/healthcareai-examples.git#subdirectory=package"
ok "venv: $VENV  ($(python -V))"

# ============================================================================
step 14 "Resolve CxrReportGen from the Foundry model catalog"
# ============================================================================
note "The 'azureml' system registry is the backing store for Foundry's model"
note "catalog. The AML SDK is the lingua franca; the model returned here is"
note "the same artifact you would see by clicking CxrReportGen in ai.azure.com."
python3 - <<'PYEOF'
import os
from azure.identity import AzureCliCredential
from azure.ai.ml import MLClient
cred = AzureCliCredential()
reg = MLClient(cred, registry_name="azureml")
m = reg.models.get(name="CxrReportGen", label="latest")
print(f"  ✓ name        = {m.name}")
print(f"  ✓ version     = {m.version}")
print(f"  ✓ id          = {m.id}")
desc = (m.description or "").splitlines()
if desc: print(f"  ✓ description = {desc[0][:90]}")
PYEOF

# ============================================================================
step 15 "Create managed online ENDPOINT (control plane only)"
# ============================================================================
note "An endpoint is a DNS-addressable URL + auth key + traffic table. It has"
note "no compute yet — the deployment in the next step adds the A100 instance."
note "Auth modes:"
note "  key     (default here) primary/secondary keys, simple to call from curl"
note "  aml_token AAD bearer tokens (recommended for production)"
python3 - <<'PYEOF'
import os
from azure.identity import AzureCliCredential
from azure.ai.ml import MLClient
from azure.ai.ml.entities import ManagedOnlineEndpoint
cred = AzureCliCredential()
mlc = MLClient(cred, os.environ["SUB"], os.environ["RG"], os.environ["PROJ"])
ep_name = os.environ["EP"]
try:
    ep = mlc.online_endpoints.get(ep_name)
    print(f"  ! endpoint '{ep_name}' already exists (state={ep.provisioning_state})")
except Exception:
    ep = ManagedOnlineEndpoint(name=ep_name, auth_mode="key",
                               description="CxrReportGen managed online endpoint")
    mlc.online_endpoints.begin_create_or_update(ep).result()
    print(f"  ✓ endpoint created: {ep_name}")
PYEOF

# ============================================================================
step 16 "Create A100 DEPLOYMENT  (~20-40 min — coffee break)"
# ============================================================================
note "This is the long one. The SDK call:"
note "  1. tells the AML control plane to provision Standard_NC24ads_A100_v4"
note "  2. pulls the CxrReportGen serving container (~15 GB) into a managed ACR"
note "  3. mounts the model files, starts the worker, waits for /health to be 200"
note "  4. routes 100% traffic to the new deployment"
note ""
note "The cost meter starts the moment the VM is provisioned (~\$3.67/hour)."
note "Liveness probe is generous (10-min initial delay) because the first model"
note "load reads ~6 GB of weights into HBM."
python3 - <<'PYEOF'
import os, time
from azure.identity import AzureCliCredential
from azure.ai.ml import MLClient
from azure.ai.ml.entities import ManagedOnlineDeployment, ProbeSettings, OnlineRequestSettings
cred = AzureCliCredential()
mlc = MLClient(cred, os.environ["SUB"], os.environ["RG"], os.environ["PROJ"])
reg = MLClient(cred, registry_name="azureml")
model = reg.models.get(name="CxrReportGen", label="latest")
dep = ManagedOnlineDeployment(
    name=os.environ["DEP"],
    endpoint_name=os.environ["EP"],
    model=model.id,
    instance_type=os.environ["SKU"],
    instance_count=1,
    request_settings=OnlineRequestSettings(
        request_timeout_ms=90000,
        max_concurrent_requests_per_instance=1,
    ),
    liveness_probe=ProbeSettings(initial_delay=600, period=60, timeout=30, failure_threshold=30),
)
t0 = time.time()
print("  · submitting deployment (this blocks; SDK polls every ~30 s)...")
mlc.online_deployments.begin_create_or_update(dep).result()
print(f"  ✓ deployment online after {(time.time()-t0)/60:.1f} min")
ep = mlc.online_endpoints.get(os.environ["EP"])
ep.traffic = {os.environ["DEP"]: 100}
mlc.online_endpoints.begin_create_or_update(ep).result()
print(f"  ✓ traffic routing: 100% -> {os.environ['DEP']}")
PYEOF

# ============================================================================
step 17 "Print endpoint URL + key (cached to disk for later runs)"
# ============================================================================
EP_URL=$(az ml online-endpoint show -g "$RG" --workspace-name "$PROJ" -n "$EP" --query scoring_uri -o tsv)
EP_KEY=$(az ml online-endpoint get-credentials -g "$RG" --workspace-name "$PROJ" -n "$EP" --query primaryKey -o tsv)
ok "scoring_uri: $EP_URL"
ok "primary_key: ${EP_KEY:0:6}...${EP_KEY: -4}  (saved to $WORKDIR/.endpoint.env)"
cat > "$WORKDIR/.endpoint.env" <<EOF
export EP_URL='$EP_URL'
export EP_KEY='$EP_KEY'
EOF
chmod 600 "$WORKDIR/.endpoint.env"

# ============================================================================
step 18 "Materialise sample DICOM files (or use what you provided)"
# ============================================================================
note "If you set DICOM_FRONTAL=/path/to/frontal.dcm and DICOM_LATERAL=/path/...,"
note "we upload those. Otherwise we synthesize 1024x1024 monochrome DICOMs with"
note "pydicom — useful for plumbing tests but the report content won't be"
note "clinically meaningful. Bring real CXR DICOMs for real outputs."
SAMPLES_DIR="$WORKDIR/samples"; mkdir -p "$SAMPLES_DIR"
if [ -n "$DICOM_FRONTAL" ] && [ -f "$DICOM_FRONTAL" ]; then
  cp "$DICOM_FRONTAL" "$SAMPLES_DIR/frontal.dcm"
  ok "using your frontal: $DICOM_FRONTAL"
else
  warn "DICOM_FRONTAL not provided — synthesizing a phantom for plumbing test."
  python3 - <<'PYEOF'
import os, numpy as np, pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import generate_uid, ExplicitVRLittleEndian
def synth(path, seed):
    rng = np.random.default_rng(seed); rows = cols = 1024
    yy, xx = np.indices((rows, cols))
    pix = ((np.sqrt((xx-512)**2 + (yy-512)**2)/512*3000)
           + rng.normal(0, 80, (rows, cols))).clip(0, 4095).astype(np.uint16)
    pix[200:800, 200:480]  = 2200  # left lung
    pix[200:800, 544:824]  = 2400  # right lung
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID    = "1.2.840.10008.5.1.4.1.1.1.1"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID          = ExplicitVRLittleEndian
    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\0"*128)
    ds.PatientName="Synthetic^Demo"; ds.PatientID="DEMO001"
    ds.Modality="DX"; ds.StudyInstanceUID=generate_uid()
    ds.SeriesInstanceUID=generate_uid(); ds.SOPInstanceUID=meta.MediaStorageSOPInstanceUID
    ds.SOPClassUID=meta.MediaStorageSOPClassUID
    ds.Rows, ds.Columns = rows, cols
    ds.BitsAllocated=16; ds.BitsStored=12; ds.HighBit=11
    ds.PixelRepresentation=0; ds.SamplesPerPixel=1
    ds.PhotometricInterpretation="MONOCHROME2"
    ds.PixelData = pix.tobytes()
    ds.is_little_endian=True; ds.is_implicit_VR=False
    ds.save_as(path)
base = os.path.join(os.environ["WORKDIR"], "samples")
synth(os.path.join(base, "frontal.dcm"), 1)
synth(os.path.join(base, "lateral.dcm"), 2)
print("  ✓ synthesized frontal.dcm + lateral.dcm")
PYEOF
fi
if [ -n "$DICOM_LATERAL" ] && [ -f "$DICOM_LATERAL" ]; then
  cp "$DICOM_LATERAL" "$SAMPLES_DIR/lateral.dcm"
  ok "using your lateral: $DICOM_LATERAL"
fi

# ============================================================================
step 19 "Upload DICOMs to blob -> ${IN_CONTAINER}/${STUDY_ID}/"
# ============================================================================
note "Using --auth-mode login so the Storage SDK picks up the RBAC role we"
note "granted ourselves in Step 11 — no account keys on the wire here."
az storage blob upload-batch -s "$SAMPLES_DIR" -d "$IN_CONTAINER/$STUDY_ID" \
  --account-name "$SA" --auth-mode login --pattern "*.dcm" --overwrite \
  --only-show-errors >/dev/null
az storage blob list --account-name "$SA" -c "$IN_CONTAINER" --auth-mode login \
   --prefix "$STUDY_ID/" --query "[].{name:name, sizeKB: properties.contentLength}" -o table 2>/dev/null || true
ok "DICOMs landed under $IN_CONTAINER/$STUDY_ID/"

# ============================================================================
step 20 "End-to-end inference  (download -> invoke -> render -> upload)"
# ============================================================================
note "Single Python block doing all four legs:"
note "  1. download DICOMs from blob via DefaultAzureCredential"
note "  2. invoke endpoint via healthcareai_toolkit (handles DICOM windowing,"
note "     PNG conversion, base64 wrapping, and parses the response)"
note "  3. render report.json + report.md + frontal_overlay.png"
note "  4. upload the three artifacts back to the output container"
python3 - <<'PYEOF'
import json, os, pathlib, tempfile, io
from azure.identity import AzureCliCredential
from azure.storage.blob import BlobServiceClient, ContentSettings
from healthcareai_toolkit.clients import CxrReportGenClient
import numpy as np, SimpleITK as sitk
from PIL import Image, ImageDraw

ACC, IN_C, OUT_C, STUDY = os.environ["SA"], os.environ["IN_CONTAINER"], os.environ["OUT_CONTAINER"], os.environ["STUDY_ID"]
cred = AzureCliCredential()
svc = BlobServiceClient(f"https://{ACC}.blob.core.windows.net", credential=cred)
inc, outc = svc.get_container_client(IN_C), svc.get_container_client(OUT_C)

with tempfile.TemporaryDirectory() as tmp:
    tmp = pathlib.Path(tmp)
    for fn in ("frontal.dcm", "lateral.dcm"):
        bc = inc.get_blob_client(f"{STUDY}/{fn}")
        if bc.exists():
            (tmp/fn).write_bytes(bc.download_blob().readall())
            print(f"  · pulled {STUDY}/{fn}  ({(tmp/fn).stat().st_size/1024:.0f} KB)")

    kwargs = dict(
        frontal_image=str(tmp/"frontal.dcm"),
        indication="Adult patient with shortness of breath x 3 days",
        technique="PA and lateral chest radiograph",
        comparison="None",
    )
    if (tmp/"lateral.dcm").exists():
        kwargs["lateral_image"] = str(tmp/"lateral.dcm")

    print(f"  · invoking endpoint {os.environ['EP']} ...")
    client = CxrReportGenClient(endpoint_name=os.environ["EP"])
    res = client.submit(**kwargs)
    findings = res[0]["output"]
    print(f"  ✓ {len(findings)} findings returned")

    structured = {
        "study_id": STUDY,
        "model": {"name": "CxrReportGen", "endpoint": os.environ["EP"]},
        "inputs": {k: kwargs.get(k) for k in ("indication","technique","comparison")},
        "findings": findings,
    }
    md = [f"# Radiology Report — {STUDY}", "",
          f"**Indication:** {kwargs['indication']}", "",
          f"**Technique:** {kwargs['technique']}", "", "## Findings", ""]
    for f in findings:
        md.append(f"- {f.get('text', f)}" if isinstance(f, dict) else f"- {f}")
    md = "\n".join(md)

    arr = sitk.GetArrayFromImage(sitk.ReadImage(str(tmp/"frontal.dcm")))[0]
    lo, hi = np.percentile(arr, [1, 99])
    img8 = np.clip((arr - lo) / max(hi-lo, 1) * 255, 0, 255).astype(np.uint8)
    pil = Image.fromarray(img8).convert("RGB")
    draw = ImageDraw.Draw(pil)
    for f in findings:
        for box in (f.get("boxes_original") or []) if isinstance(f, dict) else []:
            draw.rectangle(box, outline=(255, 64, 64), width=3)
    buf = io.BytesIO(); pil.save(buf, format="PNG"); png_bytes = buf.getvalue()

    outc.upload_blob(f"{STUDY}/report.json", json.dumps(structured, indent=2).encode(),
                     overwrite=True, content_settings=ContentSettings(content_type="application/json"))
    outc.upload_blob(f"{STUDY}/report.md", md.encode(), overwrite=True,
                     content_settings=ContentSettings(content_type="text/markdown; charset=utf-8"))
    outc.upload_blob(f"{STUDY}/frontal_overlay.png", png_bytes, overwrite=True,
                     content_settings=ContentSettings(content_type="image/png"))
    print(f"  ✓ uploaded report.json + report.md + frontal_overlay.png to {OUT_C}/{STUDY}/")
PYEOF

# ============================================================================
step 21 "Verify report artifacts in blob"
# ============================================================================
az storage blob list --account-name "$SA" -c "$OUT_CONTAINER" --auth-mode login \
   --prefix "$STUDY_ID/" \
   --query "[].{name:name, kB:properties.contentLength, modified:properties.lastModified}" -o table

# ============================================================================
step 22 "DONE  — summary + cleanup hint"
# ============================================================================
cat <<EOF

  $__g✓ End-to-end deployment + first report complete.$__0

  Foundry portal:  https://ai.azure.com   (project '$PROJ' under hub '$HUB')
  Endpoint:        $EP_URL
  Reports:         https://${SA}.blob.core.windows.net/${OUT_CONTAINER}/${STUDY_ID}/

  $__y# Stop the A100 cost meter when you're done experimenting:$__0
    az ml online-endpoint delete -g "$RG" --workspace-name "$PROJ" -n "$EP" -y

  $__y# Or torch everything (RG + hub + project + storage + reports):$__0
    az group delete -n "$RG" -y --no-wait

EOF
