#!/usr/bin/env bash
# setup-waf.sh -- AWS WAFv2 in front of the fiduciary-demo ALB: blocks known-malicious/scanner
# traffic (e.g. Shodan) at the network edge, before it ever reaches the Fargate task.
#
# WHY WAF, NOT APP CODE (Jason's call, 2026-07-27, after spotting Shodan scanning the demo in the
# Security Audit Log): Security Groups can't geo-block at all (IP/CIDR only), and a Python-side
# GeoIP implementation would need either a paid API per request or a self-maintained MaxMind
# GeoLite2 database -- an ongoing maintenance burden for something AWS already solves centrally.
# Blocking here also means malicious traffic never reaches the container at all (saves compute/
# bandwidth, smaller attack surface) instead of being denied only after a full TLS handshake +
# app-layer auth check.
#
# THREE RULES, IN PRIORITY ORDER:
#   1. AWS Managed Rule Group "AmazonIpReputationList" -- AWS's own continuously-updated list of
#      known-malicious IPs/scanners. BLOCK. This is the durable, low-maintenance defense -- AWS
#      updates it, not you.
#   2. Custom IP set -- a static CIDR blocklist you control. Seeded with 207.90.244.0/24, the exact
#      range ARIN's own WHOIS/RDAP identified as "SHODAN-01" (Net Handle NET-207-90-244-0-1) in the
#      screenshot that prompted this. BLOCK. Add more ranges to the IP set any time (see "ADDING
#      MORE RANGES" below) -- but be honest with yourself that NO static list is ever complete.
#      Shodan (and similar services -- Censys, BinaryEdge, etc.) rotates and expands scanning
#      infrastructure across many ranges over time, many undocumented; this blocks what you know
#      about TODAY, it is not a permanent guarantee against all future scanning. Rule 1 above is
#      the more durable layer for that reason.
#   3. Geo-allow rule -- BLOCK. Jason's explicit decision (2026-07-27): allow only US-based
#      connections, block everything else. Be aware of the tradeoff this makes deliberately: a
#      legitimate visitor (an interviewer, say) browsing from outside the US, or through a non-US
#      VPN exit node, is now blocked too, same as a scanner would be. If that ever turns away
#      someone you wanted to reach the site, see "CHANGING THE ALLOWED COUNTRY LIST" at the bottom
#      to add more country codes -- this is a live-blocking rule now, not a monitor-only one.
#
# Usage:
#   export AWS_PROFILE=fiduciary-demo
#   ./scripts/aws-demo/setup-waf.sh
#
# Safe to re-run: uses get-or-create for the IP set and Web ACL, updates the rule set in place.

cd "$(dirname "${BASH_SOURCE[0]}")"
source ./common.sh

WAF_NAME="fiduciary-demo-acl"
IPSET_NAME="fiduciary-demo-blocklist"
# Seeded from the ARIN RDAP lookup in Jason's screenshot (2026-07-27): "Net Range 207.90.244.0 -
# 207.90.244.255", "Name: SHODAN-01". Add more here (space-separated) as you identify them -- see
# "ADDING MORE RANGES" at the bottom of this file for the exact command, no need to re-run this
# whole script.
SEED_BLOCKLIST_CIDRS="207.90.244.0/24"

ALB="$(alb_arn)"
[ -z "$ALB" ] && die "No fiduciary-demo ALB found -- has the environment been provisioned?"

# --- Step 1: the custom IP set (get-or-create) ---

IPSET_ID="$(aws wafv2 list-ip-sets --scope REGIONAL --region "$REGION" \
  --query "IPSets[?Name=='$IPSET_NAME'].Id | [0]" --output text 2>/dev/null | sed 's/None//')"

if [ -z "$IPSET_ID" ]; then
  log "Creating IP set '$IPSET_NAME' seeded with: $SEED_BLOCKLIST_CIDRS"
  ADDR_JSON="$(python3 -c "import json,sys; print(json.dumps(sys.argv[1:]))" $SEED_BLOCKLIST_CIDRS)"
  aws wafv2 create-ip-set --name "$IPSET_NAME" --scope REGIONAL --region "$REGION" \
    --ip-address-version IPV4 --addresses "$ADDR_JSON" \
    --description "Manually-curated CIDR blocklist (e.g. observed scanner ranges like Shodan)" \
    >/dev/null
  IPSET_ID="$(aws wafv2 list-ip-sets --scope REGIONAL --region "$REGION" \
    --query "IPSets[?Name=='$IPSET_NAME'].Id | [0]" --output text)"
else
  log "IP set '$IPSET_NAME' already exists (Id: $IPSET_ID) -- leaving its current address list as-is."
  log "(Re-running this script does NOT reset your manually-added CIDRs back to just the seed.)"
fi
IPSET_ARN="$(aws wafv2 get-ip-set --name "$IPSET_NAME" --scope REGIONAL --id "$IPSET_ID" \
  --region "$REGION" --query 'IPSet.ARN' --output text)"

# --- Step 2: the Web ACL rule set ---
# GEO-ALLOW IS LIVE: allows only the country codes below, blocks everything else. Jason's explicit
# 2026-07-27 decision to restrict the public demo to US-based connections. Add ISO 3166-1 alpha-2
# codes here (e.g. "CA", "GB") if you ever want to widen this without re-reading the whole script.
GEO_ALLOWED_COUNTRIES='["US"]'

python3 - "$IPSET_ARN" "$GEO_ALLOWED_COUNTRIES" > /tmp/waf-rules.json <<'PYEOF'
import json, sys
ipset_arn = sys.argv[1]
countries = json.loads(sys.argv[2])

def vis(name):
    return {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": name}

rules = [
    {
        "Name": "AWS-AmazonIpReputationList",
        "Priority": 1,
        "Statement": {"ManagedRuleGroupStatement": {
            "VendorName": "AWS", "Name": "AWSManagedRulesAmazonIpReputationList"}},
        "OverrideAction": {"None": {}},
        "VisibilityConfig": vis("IpReputationList"),
    },
    {
        "Name": "CustomIpBlocklist",
        "Priority": 2,
        "Statement": {"IPSetReferenceStatement": {"ARN": ipset_arn}},
        "Action": {"Block": {}},
        "VisibilityConfig": vis("CustomIpBlocklist"),
    },
    {
        "Name": "GeoAllowListOnly",
        "Priority": 3,
        "Statement": {"NotStatement": {"Statement": {
            "GeoMatchStatement": {"CountryCodes": countries}}}},
        # NotStatement + CountryCodes = "match everything NOT in this list" -- combined with
        # Action=Block below, this blocks every connection whose source country ISN'T in the
        # allow-list, i.e. implements "allow only US" rather than "block a specific blocklist".
        "Action": {"Block": {}},
        "VisibilityConfig": vis("GeoAllowListOnly"),
    },
]
print(json.dumps(rules))
PYEOF

WAF_ID="$(aws wafv2 list-web-acls --scope REGIONAL --region "$REGION" \
  --query "WebACLs[?Name=='$WAF_NAME'].Id | [0]" --output text 2>/dev/null | sed 's/None//')"

if [ -z "$WAF_ID" ]; then
  log "Creating Web ACL '$WAF_NAME'..."
  aws wafv2 create-web-acl --name "$WAF_NAME" --scope REGIONAL --region "$REGION" \
    --default-action Allow={} \
    --rules file:///tmp/waf-rules.json \
    --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=fiduciary-demo-acl \
    >/dev/null
  WAF_ID="$(aws wafv2 list-web-acls --scope REGIONAL --region "$REGION" \
    --query "WebACLs[?Name=='$WAF_NAME'].Id | [0]" --output text)"
else
  log "Web ACL '$WAF_NAME' already exists (Id: $WAF_ID) -- updating its rule set in place."
  LOCK_TOKEN="$(aws wafv2 get-web-acl --name "$WAF_NAME" --scope REGIONAL --id "$WAF_ID" \
    --region "$REGION" --query 'LockToken' --output text)"
  aws wafv2 update-web-acl --name "$WAF_NAME" --scope REGIONAL --id "$WAF_ID" --region "$REGION" \
    --default-action Allow={} \
    --rules file:///tmp/waf-rules.json \
    --visibility-config SampledRequestsEnabled=true,CloudWatchMetricsEnabled=true,MetricName=fiduciary-demo-acl \
    --lock-token "$LOCK_TOKEN" >/dev/null
fi
WAF_ARN="$(aws wafv2 get-web-acl --name "$WAF_NAME" --scope REGIONAL --id "$WAF_ID" \
  --region "$REGION" --query 'WebACL.ARN' --output text)"

# --- Step 3: attach to the ALB (idempotent -- associating an already-associated resource is a no-op) ---
log "Associating Web ACL with the demo ALB..."
aws wafv2 associate-web-acl --web-acl-arn "$WAF_ARN" --resource-arn "$ALB"

echo
echo "Done. All 3 rules are BLOCKING now: (1) AWS IP reputation list, (2) custom blocklist incl."
echo "207.90.244.0/24 (the observed Shodan range), (3) geo-allow -- ONLY US-based connections reach"
echo "the site; everything else, including a legitimate non-US visitor, is now blocked too."
echo
echo "Verify:"
echo "  aws wafv2 get-web-acl --name $WAF_NAME --scope REGIONAL --id $WAF_ID --region $REGION"
echo "  # CloudWatch console -> WAF & Shield -> $WAF_NAME -> Sampled requests, to see what's"
echo "  # actually being matched/blocked in near-real-time."
echo
echo "Cost: ~\$5/month (Web ACL) + \$1/month per rule (3 rules = \$3) + \$0.60 per million requests"
echo "inspected -- for a low-traffic demo site this is a few dollars a month, not a meaningful line"
echo "item next to the NAT Gateway/ALB/EFS baseline already documented in §16."
echo
echo "ADDING MORE RANGES to the blocklist later, without re-running this whole script:"
echo "  aws wafv2 get-ip-set --name $IPSET_NAME --scope REGIONAL --id $IPSET_ID --region $REGION"
echo "  # note the current Addresses list + LockToken, then:"
echo "  aws wafv2 update-ip-set --name $IPSET_NAME --scope REGIONAL --id $IPSET_ID --region $REGION \\"
echo "    --addresses '[\"207.90.244.0/24\",\"<new-cidr-here>\"]' --lock-token <LockToken-from-above>"
echo
echo "WIDENING THE ALLOWED COUNTRY LIST later (e.g. if a non-US visitor gets blocked and shouldn't"
echo "be): edit GEO_ALLOWED_COUNTRIES above (ISO 3166-1 alpha-2 codes, e.g. add \"CA\" or \"GB\"),"
echo "then re-run this script."
echo
echo "To remove entirely (e.g. before a full §15 teardown):"
echo "  aws wafv2 disassociate-web-acl --resource-arn $ALB"
echo "  aws wafv2 delete-web-acl --name $WAF_NAME --scope REGIONAL --id $WAF_ID --lock-token <LockToken>"
echo "  aws wafv2 delete-ip-set --name $IPSET_NAME --scope REGIONAL --id $IPSET_ID --lock-token <LockToken>"
