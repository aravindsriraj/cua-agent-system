#!/usr/bin/env bash
# Regenerates /evidence against live ParaBank and SauceDemo (needs GEMINI_API_KEY, PASSWORD and SAUCE_PASSWORD in .env).
set -u
cd "$(dirname "$0")/.."
# Build into artifacts/ and runs/; evidence/ is replaced only at the end, so an interrupted run never loses it.
rm -rf artifacts runs && mkdir -p runs
T=runs/transcript.txt
run() { echo "\$ $*" >> $T; "$@" >> $T 2>&1; echo "(exit $?)" >> $T; echo >> $T; }
# The AI names the inputs. Read its names (in goal order) so the commands below use them.
names() { uv run python -c "import sys; from cua import artifact as A
print(' '.join(k for k, v in A.load(sys.argv[1]).inputs.items() if v.type != 'secret'))" "$1"; }
C=parabank-account-balance

# 1. Discovery: the AI reads the plain-English goal, drives the browser, and reviews the recording.
run uv run cua record https://parabank.parasoft.com/parabank/index.htm \
  "Log in as cua_demo_6955 with {password:secret}, open account 31437 and read its balance" --name $C --headless
run uv run cua show $C
read UNAME ACCT <<< "$(names $C)"

# 2. Replay (code only): success, another input, a record that does not exist, a missing input.
for a in 31437 31770 99999; do run uv run cua run $C "$UNAME=cua_demo_6955" "$ACCT=$a" --headless; done
run uv run cua run $C "$UNAME=cua_demo_6955" --headless

# 3. Something new, with --ai: the AI decides once and it is remembered; the second time code handles it, no AI.
for f in modal expire_session; do
  run uv run cua run $C "$UNAME=cua_demo_6955" "$ACCT=31437" --inject $f@s5 --ai --headless
  run uv run cua run $C "$UNAME=cua_demo_6955" "$ACCT=31437" --inject $f@s5 --headless
done
run uv run cua run $C "$UNAME=cua_demo_6955" "$ACCT=31437" --inject http500@s5 --headless     # code: retry

# 4. Default replay, no human: an unknown screen fails with evidence (v1 does not know the pop-up).
run uv run cua run $C "$UNAME=cua_demo_6955" "$ACCT=31437" --version 1 --inject modal@s5 --headless
# 5. Default replay, a human takes over the same live session, closes the pop-up and labels it -> remembered.
run uv run python scripts/operator_demo.py handoff
run uv run cua show $C

# 6. Risky flow: recording pauses for approval; a draft will not transfer unattended; approved, it does.
run uv run python scripts/operator_demo.py transfer
run uv run cua show parabank-transfer
read TUSER TAMT TFROM TTO <<< "$(names parabank-transfer)"
run uv run cua run parabank-transfer "$TUSER=cua_demo_6955" "$TAMT=1" "$TFROM=31437" "$TTO=31770" --headless
run uv run cua approve parabank-transfer --by reviewer
run uv run cua run parabank-transfer "$TUSER=cua_demo_6955" "$TAMT=1" "$TFROM=31437" "$TTO=31770" --headless

# 7. Not just banks: the same system on an unrelated web app (SauceDemo shop).
S=saucedemo-checkout
run uv run cua record https://www.saucedemo.com/ \
  "Log in as standard_user with {sauce_password:secret}, add the Sauce Labs Backpack to the cart, check out with first name Ada, last name Lovelace and postal code 10001, reach the checkout overview page and read the item total" \
  --name $S --headless
run uv run cua show $S
read SUSER SITEM SFIRST SLAST SZIP <<< "$(names $S)"
for it in "Sauce Labs Backpack" "Sauce Labs Bike Light" "Sauce Labs Spaceship"; do
  run uv run cua run $S "$SUSER=standard_user" "$SITEM=$it" "$SFIRST=Grace" "$SLAST=Hopper" "$SZIP=94016" --headless
done
run uv run cua list

rm -rf evidence/runs evidence/artifacts evidence/transcript.txt
mv runs/transcript.txt evidence/transcript.txt
cp -R artifacts evidence/artifacts
cp -R runs evidence/runs
rm -rf artifacts runs
