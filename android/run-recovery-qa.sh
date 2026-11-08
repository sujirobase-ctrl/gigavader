#!/usr/bin/env bash
set -euo pipefail

python android/qa_server.py &
server_pid=$!
trap 'kill "$server_pid" 2>/dev/null || true' EXIT
for attempt in {1..20}; do
    if curl -fsS -o /dev/null http://127.0.0.1:8876/threads/resume; then
        break
    fi
    sleep 1
done
curl -fsS -o /dev/null http://127.0.0.1:8876/threads/resume
adb reverse tcp:8876 tcp:8876
adb install -r android/app/build/outputs/apk/qa/app-qa.apk
adb install -r android/app/build/outputs/apk/androidTest/qa/app-qa-androidTest.apk

run_test() {
    local output
    output="$(adb shell am instrument -w -e class "com.sujirobase.buidl.RecoveryTest#$1" \
        com.sujirobase.buidl.qa.test/androidx.test.runner.AndroidJUnitRunner)"
    printf '%s\n' "$output"
    grep -Fq 'OK (1 test)' <<< "$output"
}

run_test prepareInterruptedDownload
adb shell am force-stop com.sujirobase.buidl.qa
run_test restoreAfterProcessDeath
grep -Eq '"range": "bytes=[1-9][0-9]*-"' android/qa/requests.jsonl
