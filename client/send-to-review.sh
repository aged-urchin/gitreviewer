#!/usr/bin/env bash
#
# send-to-review.sh — Submit code to GitReviewer for review.
#
# Usage:
#   ./send-to-review.sh -d "review the last 3 commits"
#   ./send-to-review.sh -d "review uncommitted changes" -l
#   ./send-to-review.sh                                      # default: review last commit
#   ./send-to-review.sh -e                                   # end session after review

# set -e 太严格，API 调用偶发失败会导致脚本静默退出，改为显式检查

# ------------------------------------------------------------------ config --
SERVER="http://localhost:8000"
DES=""
LOCAL=false
NO_POLL=false
END_SESSION=false
POLL_INTERVAL=2
TIMEOUT=300

# ------------------------------------------------------------------ helpers --
RED='\033[0;31m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
WHITE='\033[0;37m'
NC='\033[0m'

color() { printf "%b%s%b\n" "$1" "$2" "$NC"; }

# Simple JSON value extractor (no jq dependency)
json_val() {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',''))" 2>/dev/null
}
json_val_nested() {
    # $1 = key, $2 = sub-key
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',{}).get('$2',''))" 2>/dev/null
}
json_count() {
    python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('$1',[])))" 2>/dev/null
}
json_index() {
    # $1 = array_key, $2 = index, $3 = field
    python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1',[])[$2].get('$3',''))" 2>/dev/null
}
# Escape a string for JSON
json_escape() {
    python3 -c "import sys,json; print(json.dumps(sys.argv[1]))" "$1"
}

usage() {
    cat << 'EOF'
Usage: send-to-review.sh [OPTIONS]

Options:
  -d, --des TEXT        Review description / instruction
  -s, --server URL      Server URL (default: http://localhost:8000)
  -l, --local           Include uncommitted local changes (requires -d)
  -n, --no-poll         Submit only, do not wait for result
  -e, --end-session     End session after review
  -p, --poll-interval N Poll interval in seconds (default: 5)
  -h, --help            Show this help

Examples:
  ./send-to-review.sh -d "review the last 3 commits"
  ./send-to-review.sh -d "review uncommitted changes" -l
  ./send-to-review.sh -d "find design flaws" -s http://gitreviewer-pc:8000
EOF
    exit 0
}

# ------------------------------------------------------------------ parse args --
while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--des)          DES="$2"; shift 2 ;;
        -s|--server)       SERVER="$2"; shift 2 ;;
        -l|--local)        LOCAL=true; shift ;;
        -n|--no-poll)      NO_POLL=true; shift ;;
        -e|--end-session)  END_SESSION=true; shift ;;
        -p|--poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
        -h|--help)         usage ;;
        *) color "$RED" "Unknown option: $1"; usage ;;
    esac
done

# ------------------------------------------------------------------ validate --
if ! command -v git &>/dev/null; then
    color "$RED" "ERROR: git not found on PATH"; exit 1
fi

GIT_REMOTE=$(git remote get-url origin 2>/dev/null || true)
if [[ -z "$GIT_REMOTE" ]]; then
    color "$RED" "ERROR: no origin remote"; exit 1
fi

GIT_BRANCH=$(git branch --show-current 2>/dev/null || true)
if [[ -z "$GIT_BRANCH" ]]; then
    color "$RED" "ERROR: not on a branch"; exit 1
fi

if $LOCAL && [[ -z "$DES" ]]; then
    color "$RED" "ERROR: -l/--local requires -d/--des to describe what was changed"; exit 1
fi

if [[ -z "$DES" ]] && ! $LOCAL; then
    DES="Review the last commit (git diff HEAD~1)"
    color "$GRAY" "Default: review last commit"
fi

# ------------------------------------------------------------------ api helper --
api() {
    local method="$1" path="$2" data="$3" timeout="$4"
    local url="${SERVER}${path}"
    if [[ -n "$data" ]]; then
        curl -s -X "$method" "$url" -H "Content-Type: application/json" -d "$data" --max-time "$timeout"
    else
        curl -s -X "$method" "$url" -H "Content-Type: application/json" --max-time "$timeout"
    fi
}

# ------------------------------------------------------------------ session --
SESSION_FILE="$(pwd)/.gitreviewer_session"
SESSION_ID=""

if [[ -f "$SESSION_FILE" ]]; then
    SESSION_ID=$(cat "$SESSION_FILE" | json_val session_id)
    CACHED_SERVER=$(cat "$SESSION_FILE" | json_val server)
    if [[ -n "$CACHED_SERVER" ]] && ! echo "$*" | grep -q -- '-s\|--server'; then
        SERVER="$CACHED_SERVER"
    fi
    color "$GRAY" "Session: $SESSION_ID @ $SERVER"

    RESP=$(api GET "/api/v1/sessions/$SESSION_ID" "" 10)
    STATUS=$(echo "$RESP" | json_val status)
    if [[ "$STATUS" == "closed" || -z "$STATUS" ]]; then
        color "$YELLOW" "Session gone, creating new..."
        rm -f "$SESSION_FILE"
        SESSION_ID=""
    elif [[ "$STATUS" != "ready" ]]; then
        color "$YELLOW" "Session status: $STATUS, creating new..."
        rm -f "$SESSION_FILE"
        SESSION_ID=""
    fi
fi

if [[ -z "$SESSION_ID" ]]; then
    color "$CYAN" "Creating session for $GIT_REMOTE ($GIT_BRANCH) @ $SERVER ..."
    RESP=$(api POST "/api/v1/sessions" "{\"git_url\":\"$GIT_REMOTE\",\"branch\":\"$GIT_BRANCH\"}" 120)
    SESSION_ID=$(echo "$RESP" | json_val session_id)
    if [[ -z "$SESSION_ID" ]]; then
        color "$RED" "ERROR: Failed to create session. Response: $RESP"; exit 1
    fi
    SESSION_STATUS=$(echo "$RESP" | json_val status)
    echo "{\"session_id\":\"$SESSION_ID\",\"server\":\"$SERVER\"}" > "$SESSION_FILE"
    color "$GREEN" "Session: $SESSION_ID ($SESSION_STATUS)"
fi

# ------------------------------------------------------------------ submit --
color "$CYAN" "Submitting review..."

if $LOCAL; then
    PATCH=$(git diff HEAD 2>/dev/null || true)
    if [[ -z "${PATCH// }" ]]; then
        color "$YELLOW" "No local changes"; exit 0
    fi
    PATCH_ESCAPED=$(json_escape "$PATCH")
    DESC_ESCAPED=$(json_escape "$DES")
    BODY="{\"description\":$DESC_ESCAPED,\"patch\":$PATCH_ESCAPED"
    if $NO_POLL; then BODY="$BODY,\"no_poll\":true"; fi
    BODY="$BODY}"
    color "$YELLOW" "Including local diff ($(echo "$PATCH" | wc -c) chars)"
else
    DESC_ESCAPED=$(json_escape "$DES")
    BODY="{\"description\":$DESC_ESCAPED"
    if $NO_POLL; then BODY="$BODY,\"no_poll\":true"; fi
    BODY="$BODY}"
fi

RESP=$(api POST "/api/v1/sessions/$SESSION_ID/reviews" "$BODY" 30)
REVIEW_ID=$(echo "$RESP" | json_val review_id)
if [[ -z "$REVIEW_ID" ]]; then
    color "$RED" "ERROR: Failed to submit review. Response: $RESP"; exit 1
fi
REVIEW_STATUS=$(echo "$RESP" | json_val status)
color "$GREEN" "Review: $REVIEW_ID ($REVIEW_STATUS)"

# ------------------------------------------------------------------ poll --
if $NO_POLL; then
    color "$CYAN" "URL: $SERVER/api/v1/sessions/$SESSION_ID/reviews/$REVIEW_ID"
    exit 0
fi

color "$CYAN" "Waiting..."
WAITED=0
PREV_STATUS="queued"

while true; do
    sleep "$POLL_INTERVAL"
    WAITED=$((WAITED + POLL_INTERVAL))
    RESP=$(api GET "/api/v1/sessions/$SESSION_ID/reviews/$REVIEW_ID" "" 10)
    STATUS=$(echo "$RESP" | json_val status)
    if [[ "$STATUS" != "$PREV_STATUS" ]]; then
        printf "\n%s->%s" "$PREV_STATUS" "$STATUS"
        PREV_STATUS="$STATUS"
    else
        printf "."
    fi
    if [[ "$STATUS" == "completed" || "$STATUS" == "failed" || "$STATUS" == "cancelled" ]]; then
        break
    fi
    if [[ $WAITED -ge $TIMEOUT ]]; then
        printf "\n"
        color "$YELLOW" "Timeout (${WAITED}s)"
        color "$CYAN" "Check later: GET $SERVER/api/v1/sessions/$SESSION_ID/reviews/$REVIEW_ID"
        exit 0
    fi
done

echo ""

# ------------------------------------------------------------------ results --
if [[ "$STATUS" == "failed" ]]; then
    ERROR=$(echo "$RESP" | json_val error)
    color "$RED" "Review FAILED: ${ERROR:-unknown}"
    exit 1
fi

SCOPE=$(echo "$RESP" | json_val scope)
SUMMARY=$(echo "$RESP" | json_val summary)
FINDING_COUNT=$(echo "$RESP" | json_count findings)

printf "%b========================================%b\n" "$WHITE" "$NC"
printf "%b  Review Complete%b\n" "$WHITE" "$NC"
printf "%b========================================%b\n" "$WHITE" "$NC"
if [[ -n "$SCOPE" ]]; then printf "%bScope: %s%b\n" "$GRAY" "$SCOPE" "$NC"; fi
printf "%bSummary: %s%b\n" "$CYAN" "$SUMMARY" "$NC"
printf "%bFindings: %s%b\n" "$YELLOW" "$FINDING_COUNT" "$NC"
printf "%b========================================%b\n" "$WHITE" "$NC"

if [[ "$FINDING_COUNT" -gt 0 ]]; then
    echo ""
    for (( i=0; i<FINDING_COUNT; i++ )); do
        SEV=$(echo "$RESP" | json_index findings "$i" severity)
        FILE=$(echo "$RESP" | json_index findings "$i" file)
        LINE=$(echo "$RESP" | json_index findings "$i" line)
        TITLE=$(echo "$RESP" | json_index findings "$i" title)
        CAT=$(echo "$RESP" | json_index findings "$i" category)
        DESC=$(echo "$RESP" | json_index findings "$i" description)
        SUGG=$(echo "$RESP" | json_index findings "$i" suggestion)

        case "$SEV" in
            high)   SEV_COLOR="$RED" ;;
            medium) SEV_COLOR="$YELLOW" ;;
            *)      SEV_COLOR="$GRAY" ;;
        esac

        printf "%b[%s]%b %s:%s - %s\n" "$SEV_COLOR" "$(echo "$SEV" | tr '[:lower:]' '[:upper:]')" "$NC" "$FILE" "$LINE" "$TITLE"
        printf "%b  Category: %s  Problem: %s  Fix: %s%b\n" "$WHITE" "$CAT" "$DESC" "$SUGG" "$NC"
        echo ""
    done

    HIGH=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='high'))")
    MEDIUM=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='medium'))")
    LOW=$(echo "$RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); print(sum(1 for f in d.get('findings',[]) if f.get('severity')=='low'))")
    printf "%bSummary: %s high, %s medium, %s low%b\n" "$WHITE" "$HIGH" "$MEDIUM" "$LOW" "$NC"
fi

# ------------------------------------------------------------------ end session --
if $END_SESSION; then
    echo ""
    color "$CYAN" "Ending session..."
    api DELETE "/api/v1/sessions/$SESSION_ID" "" 10 >/dev/null
    rm -f "$SESSION_FILE"
    color "$GREEN" "Session ended"
fi
