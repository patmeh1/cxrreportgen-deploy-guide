# CXRReportGen on Azure — Deployment & Cost Guide

A static, multi-tab GitHub Pages site that walks customers through deploying Microsoft's
**CXRReportGen** healthcare AI model on Azure: from prerequisites and the DICOM
ingestion pipeline through to the production deployment, security posture, and a
client-side **cost calculator** that incorporates DICOM volume, networking, Foundry
add-ons, and reservation discounts.

**Live site:** https://patmeh1.github.io/cxrreportgen-deploy-guide/

> ⚠️ Research only. CXRReportGen is not for clinical diagnosis or treatment. See the
> Compliance & Responsible AI tab.

## Tabs

1. Overview · 2. Prerequisites · 3. Architecture · 4. DICOM Ingestion ·
5. Deploy (Studio / SDK / CLI / `azd`) · 6. Test the Endpoint ·
7. Networking & Security · 8. Compliance & Responsible AI ·
9. **Cost Calculator** · 10. Monitoring & Ops · 11. Troubleshooting · 12. References

## Local preview

```bash
cd docs
python3 -m http.server 8080
open http://localhost:8080
```

## Publish to GitHub Pages

```bash
git init -b main
git add . && git commit -m "Initial site"
gh repo create patmeh1/cxrreportgen-deploy-guide --public --source=. --remote=origin --push
gh api -X PATCH repos/patmeh1/cxrreportgen-deploy-guide/pages \
  -f source.branch=main -f source.path=/docs || \
  gh api -X POST repos/patmeh1/cxrreportgen-deploy-guide/pages \
  -f source[branch]=main -f source[path]=/docs
```

After ~30 seconds the site is live at `https://patmeh1.github.io/cxrreportgen-deploy-guide/`.

## Quarterly maintenance

| File | What to refresh |
|---|---|
| `docs/assets/data/pricing.json` | Re-check every line item against the [Azure Pricing Calculator](https://azure.microsoft.com/pricing/calculator/). Bump `_meta.last_verified`. |
| `docs/assets/data/region-availability.json` | Verify the A100 v4 SKU + Foundry availability per region. |
| `docs/assets/samples/deploy.py` and the Deploy tab | Re-validate against the upstream [`cxr-deploy.ipynb`](https://github.com/Azure/azureml-examples/blob/main/sdk/python/foundation-models/healthcare-ai/cxrreportgen/cxr-deploy.ipynb) and bump the model `label` if the team has cut a new version. |
| References tab | Spot-check links; bump the "verified" date. |

## License

MIT — see [LICENSE](LICENSE).
