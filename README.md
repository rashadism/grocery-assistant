# Grocery shopping assistant on Amazon Bedrock AgentCore and OpenChoreo

A Strands Agents grocery assistant on OpenChoreo, modeling three Bedrock AgentCore primitives as platform `Resource`s, Bedrock model access, AgentCore Memory for per-user taste recall, and the AgentCore Browser Tool for live Instacart search, with a human take-over via Live View on sign-in walls. Two Components on OpenChoreo's built-in `service` ComponentType, `agent` and `frontend`. Tested on local `k3d-openchoreo`, `openchoreo.dev/v1alpha1`, ACK Bedrock controller `1.4.1`, AWS `us-east-1`, arm64.

This repo ships the reusable pieces only, `agent/`, `frontend/`, `platform/`, not the account-specific Project/Resource/Component YAML. Design rationale and governance rules are in [AGENTS.md](AGENTS.md).

## Reproduce

You need `aws`, `kubectl`, `helm`, `docker`, and `jq`, an AWS identity allowed to create Bedrock inference profiles, AgentCore Memory/Browser resources, and demo IAM users/keys, and a running OpenChoreo install with its `default` pipeline. No cluster yet? [The k3d guide](https://openchoreo.dev/docs/getting-started/try-it-out/on-k3d-locally/) gets you `k3d-openchoreo`, the context every command below assumes. Run from this directory. Check `kubectl config current-context` and `aws sts get-caller-identity` first.

1. Install the ACK Bedrock controller, then publish the three `ClusterResourceType`s and the RBAC that lets ACK manage them.

   ```sh
   export AWS_REGION=us-east-1
   export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

   helm upgrade --install ack-bedrock oci://public.ecr.aws/aws-controllers-k8s/bedrock-chart --version 1.4.1 -n ack-system --create-namespace

   sed -e "s/__AWS_REGION__/$AWS_REGION/g" -e "s/__AWS_ACCOUNT_ID__/$AWS_ACCOUNT_ID/g" \
     platform/bedrock-model-resource-type.yaml | kubectl --context k3d-openchoreo --namespace default apply -f -
   kubectl --context k3d-openchoreo --namespace default apply -f platform/ack-cluster-agent-rbac.yaml

   kubectl --context k3d-openchoreo --namespace default apply -f platform/agentcore-memory-resource-type.yaml
   kubectl --context k3d-openchoreo --namespace default apply -f platform/ack-agentcore-cluster-agent-rbac.yaml

   kubectl --context k3d-openchoreo --namespace default apply -f platform/agentcore-browser-resource-type.yaml
   ```

   Memory and the Browser Tool provision through ACK's `bedrockagentcorecontrol` controller, separate from the Bedrock chart above. See [ACK's own docs on configuring IAM permissions](https://aws-controllers-k8s.github.io/docs/guides/configure-iam/) for what it needs.

2. Through the self-service catalog, create a Project named `grocery-assistant` on the default DeploymentPipeline and bind it to `development`. Create the three Resources (`model`, `memory` with at least one strategy, e.g. a `semantic` strategy, and `browser`), then bind `model` and `browser` to `development`, picking a model and confirming the region. Wait for `projectreleasebinding/grocery-assistant-development` and all three `resourcereleasebinding`s to go `Ready`.

3. Mint the Bedrock API key and the AgentCore credentials (IAM user + Secret `agent` reads at startup). This is the one genuinely ugly step in this whole demo, and it only exists because k3d pods don't get real AWS identity the way a cluster with IRSA or Pod Identity would. There, you'd bind that identity to the pod and skip this step entirely. Here, a script mints two IAM users and some long-lived keys instead.

   ```sh
   export AWS_REGION=us-east-1
   export PROJECT=grocery-assistant ENVIRONMENT=development
   ./scripts/mint-credentials.sh
   ```

   The Bedrock credential expires in seven days by design. Rerun the script any time to rotate both credentials, then restart the `agent` Deployment, since env vars are only read at pod start.

4. Build and publish both images. `ttl.sh` tags expire after an hour, so rebuild close to when you'll actually demo.

   ```sh
   export AGENT_IMAGE=ttl.sh/grocery-assistant-agent-$(date +%s):1h
   docker build --platform linux/arm64 -t "$AGENT_IMAGE" agent && docker push "$AGENT_IMAGE"

   export FRONTEND_IMAGE=ttl.sh/grocery-assistant-frontend-$(date +%s):1h
   docker build --platform linux/arm64 -t "$FRONTEND_IMAGE" frontend && docker push "$FRONTEND_IMAGE"
   ```

   Create the two Components (`agent`, `frontend`) through the self-service catalog, on the built-in `service` ComponentType. For each, the Deploy tab's Set up and Create release opens a Workload tab (paste in its image, port 8080) and a Dependencies tab.

   `agent` needs a Resource Dependency on all three Resources, each output bound to the env var its code reads.

   - model's `modelId` -> `BEDROCK_MODEL_ID`, `region` -> `AWS_REGION`
   - memory's `memoryId` -> `MEMORY_ID`, `accessKeyId` -> `AWS_ACCESS_KEY_ID`, `secretAccessKey` -> `AWS_SECRET_ACCESS_KEY`
   - browser's `browserId` -> `BROWSER_ID`

   `frontend` gets a Component Dependency instead, pick `agent`, bind its endpoint to `BACKEND_URL`.

5. Open `http://development-default.openchoreoapis.localhost:19080/frontend-endpoint-1/` on the local k3d install, or verify without a browser.

   ```sh
   curl -H 'Host: development-default.openchoreoapis.localhost' http://127.0.0.1:19080/frontend-endpoint-1/
   curl -H 'Host: development-default.openchoreoapis.localhost' -X POST -H 'Content-Type: application/json' \
     -d '{"username":"demo"}' http://127.0.0.1:19080/agent-endpoint-1/api/session
   ```

Run `python3 -m unittest discover -s agent -p 'test_*.py'` and `python3 -m unittest discover -s frontend -p 'test_*.py'` for the agent/tools logic and the frontend's gateway routing.

## Notes

- Per-user state (`agent/tools.py`'s `BrowserSession`) lives in app code, never the Resource. `agent` never exposes credentials to the browser.
- Full governance rationale is in [AGENTS.md](AGENTS.md).
- Long-lived keys and IAM users here are demo-only, a stand-in for the workload identity a real cluster would give the pod directly.

## If something stalls

`kubectl --context k3d-openchoreo --namespace default get resourcereleasebinding <name> -o yaml`, then the matching ACK object in `$DP_NAMESPACE`, then `kubectl --context k3d-openchoreo --namespace ack-system logs deployment/ack-bedrock-bedrock-chart`. A `ResourceApplyFailed` on `inferenceprofiles`/`memories`/`browsers` means the RBAC from step 1 is missing. `scripts/mint-credentials.sh` is safe to rerun any time to rotate. Restart the `agent` Deployment afterward since env vars are only read at pod start.

## Clean up

Delete the Components and Resources the way you created them (self-service catalog or `kubectl delete -f`). Remove the IAM users this created, tagged `grocery-assistant-demo:true` (`<project>-bedrock-profile`, `<project>-agentcore-agent`, plus the ACK controller's own user), by deleting their keys/credentials, then the user. `ttl.sh` images expire on their own.

## Further reading

[AWS application inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-application-inference-profiles.html), [CreateInferenceProfile API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_CreateInferenceProfile.html), [Bedrock API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html), [AgentCore Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory.html), [AgentCore Browser Tool](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/browser-tool.html), [ACK controllers](https://aws-controllers-k8s.github.io/docs/services/).
