#!/usr/bin/env bash
# Mints the Bedrock API key and the AgentCore IAM credentials the agent reads
# at startup, and applies them as Secrets on the data plane. Only needed
# because k3d pods have no real AWS identity (no IRSA, no Pod Identity) - on
# a cluster that has one, skip this entirely and bind that identity instead.
#
# Requires: aws, kubectl, jq. Safe to rerun to rotate both credentials; the
# agent Deployment needs a restart afterward since env vars are only read at
# pod start.
set -euo pipefail

CONTEXT=k3d-openchoreo
PROJECT=${PROJECT:-grocery-assistant}
ENVIRONMENT=${ENVIRONMENT:-development}
AWS_REGION=${AWS_REGION:-us-east-1}

PROFILE_ARN=$(kubectl --context "$CONTEXT" --namespace default \
  get resourcereleasebinding "model-${ENVIRONMENT}" -o jsonpath='{.status.outputs[?(@.name=="modelId")].value}')
MODEL_REGION=$(kubectl --context "$CONTEXT" --namespace default \
  get resourcereleasebinding "model-${ENVIRONMENT}" -o jsonpath='{.status.outputs[?(@.name=="region")].value}')
API_KEY_SECRET=$(kubectl --context "$CONTEXT" --namespace default \
  get resourcereleasebinding "model-${ENVIRONMENT}" -o jsonpath='{.status.outputs[?(@.name=="apiKey")].secretKeyRef.name}')
DP_NAMESPACE=$(kubectl --context "$CONTEXT" --namespace default \
  get projectreleasebinding "${PROJECT}-${ENVIRONMENT}" -o jsonpath='{.status.namespace}')
MODELS=$(aws bedrock get-inference-profile --inference-profile-identifier "$PROFILE_ARN" \
  --region "$MODEL_REGION" --query 'models[].modelArn' --output json)

# Bedrock API key, scoped to this one inference profile and the models
# behind it.
BEDROCK_USER="${PROJECT}-bedrock-profile"
aws iam create-user --user-name "$BEDROCK_USER" --tags Key=grocery-assistant-demo,Value=true 2>/dev/null || true
aws iam put-user-policy --user-name "$BEDROCK_USER" --policy-name profile-only --policy-document "$(jq -n \
  --arg profile "$PROFILE_ARN" --argjson models "$MODELS" '{Version:"2012-10-17",Statement:[
    {Effect:"Allow",Action:"bedrock:CallWithBearerToken",Resource:"*",Condition:{StringEquals:{"bedrock:bearerTokenType":"LONG_TERM"}}},
    {Effect:"Allow",Action:["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],Resource:[$profile]},
    {Effect:"Allow",Action:["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],Resource:$models,Condition:{StringEquals:{"bedrock:InferenceProfileArn":$profile}}}
  ]}')"

API_KEY=$(aws iam create-service-specific-credential --user-name "$BEDROCK_USER" \
  --service-name bedrock.amazonaws.com --credential-age-days 7 \
  --query 'ServiceSpecificCredential.ServiceCredentialSecret' --output text)
printf %s "$API_KEY" \
  | kubectl --context "$CONTEXT" --namespace "$DP_NAMESPACE" create secret generic "$API_KEY_SECRET" \
      --from-file=api-key=/dev/stdin --dry-run=client -o yaml \
  | kubectl --context "$CONTEXT" --namespace "$DP_NAMESPACE" apply -f -
unset API_KEY

# AgentCore credentials, scoped to this project's Memory store and Browser.
MEMORY_ARN=$(kubectl --context "$CONTEXT" --namespace default \
  get resourcereleasebinding "memory-${ENVIRONMENT}" -o jsonpath='{.status.outputs[?(@.name=="memoryId")].value}')
BROWSER_ARN=$(kubectl --context "$CONTEXT" --namespace default \
  get resourcereleasebinding "browser-${ENVIRONMENT}" -o jsonpath='{.status.outputs[?(@.name=="browserId")].value}')

AGENTCORE_USER="${PROJECT}-agentcore-agent"
aws iam create-user --user-name "$AGENTCORE_USER" --tags Key=grocery-assistant-demo,Value=true 2>/dev/null || true
aws iam put-user-policy --user-name "$AGENTCORE_USER" --policy-name agentcore-data-plane --policy-document "$(jq -n \
  --arg memory "$MEMORY_ARN" --arg browser "$BROWSER_ARN" \
  '{Version:"2012-10-17",Statement:[{Effect:"Allow",Action:"bedrock-agentcore:*",Resource:[$memory,$browser]}]}')"
aws iam put-user-policy --user-name "$AGENTCORE_USER" --policy-name bedrock-invoke --policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": ["arn:aws:bedrock:'"$AWS_REGION"':*:inference-profile/*", "arn:aws:bedrock:'"$AWS_REGION"'::foundation-model/*"]}]
}'
KEY=$(aws iam create-access-key --user-name "$AGENTCORE_USER")
AWS_ACCESS_KEY_ID=$(jq -r '.AccessKey.AccessKeyId' <<<"$KEY")
AWS_SECRET_ACCESS_KEY=$(jq -r '.AccessKey.SecretAccessKey' <<<"$KEY")

sed -e "s/__PROJECT__/$PROJECT/" -e "s/__ENVIRONMENT__/$ENVIRONMENT/" -e "s/__DP_NAMESPACE__/$DP_NAMESPACE/" \
    -e "s/__AWS_ACCESS_KEY_ID__/$AWS_ACCESS_KEY_ID/" -e "s/__AWS_SECRET_ACCESS_KEY__/$AWS_SECRET_ACCESS_KEY/" \
  "$(dirname "$0")/../platform/agentcore-credentials-secret.yaml" \
  | kubectl --context "$CONTEXT" apply -f -
unset KEY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

echo "Done. Both credentials minted and applied to namespace $DP_NAMESPACE."
