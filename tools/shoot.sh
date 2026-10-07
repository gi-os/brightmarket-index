#!/usr/bin/env bash
set -u
OUT=out
PKG=$(grep -o "^package: name='[^']*'" $OUT/badging.txt | cut -d"'" -f2)
echo "package $PKG"
adb shell settings put global verifier_verify_adb_installs 0 || true
adb shell settings put global package_verifier_enable 0 || true
adb shell wm density 420
adb shell svc power stayon true
adb shell settings put global hide_error_dialogs 1
adb shell settings put global show_first_crash_dialog 0
sleep 25
adb shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS >/dev/null 2>&1
adb shell settings put system screen_off_timeout 1800000
adb shell input keyevent 82
adb shell cmd uimode night yes || true
# Clean status bar
adb shell settings put global sysui_demo_allowed 1
adb shell am broadcast -a com.android.systemui.demo -e command enter >/dev/null
adb shell am broadcast -a com.android.systemui.demo -e command clock -e hhmm 0941 >/dev/null
adb shell am broadcast -a com.android.systemui.demo -e command battery -e level 100 -e plugged false >/dev/null
adb shell am broadcast -a com.android.systemui.demo -e command network -e wifi show -e level 4 >/dev/null
adb shell am broadcast -a com.android.systemui.demo -e command notifications -e visible false >/dev/null

adb install -r -g -t app.apk 2>&1 | tail -2 | tee $OUT/install.txt
for op in SYSTEM_ALERT_WINDOW POST_NOTIFICATION GET_USAGE_STATS WRITE_SETTINGS MANAGE_EXTERNAL_STORAGE; do adb shell appops set $PKG $op allow 2>/dev/null; done
adb shell dumpsys deviceidle whitelist +$PKG >/dev/null 2>&1

N=0
shot() {  # shot label
  N=$((N+1)); local f=$(printf "%02d" $N)-$(echo "$1" | tr -c 'A-Za-z0-9\n' '-' | tr 'A-Z' 'a-z' | cut -c1-30).png
  adb exec-out screencap -p > "$OUT/$f"
  [ -s "$OUT/$f" ] || { rm -f "$OUT/$f"; N=$((N-1)); echo "secure/blank: $1" >> $OUT/blocked.txt; return 1; }
  local h=$(md5sum < "$OUT/$f" | cut -c1-12)
  if grep -q "$h" $OUT/.hashes 2>/dev/null; then rm "$OUT/$f"; N=$((N-1)); return 1; fi
  echo "$h" >> $OUT/.hashes; echo "shot $f"; return 0
}
front() { adb shell dumpsys activity activities | grep -m1 -E "topResumedActivity|mResumedActivity" ; }
launch() { adb shell am force-stop $PKG; adb shell monkey -p $PKG -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1; sleep 7; }
dump() { adb shell uiautomator dump /sdcard/ui.xml >/dev/null 2>&1; adb shell cat /sdcard/ui.xml; }

launch
front | tee $OUT/front.txt
shot home
dump > $OUT/home.xml
# Scroll the home screen once if it scrolls
adb shell input swipe 540 1000 540 400 400; sleep 2; shot home-scrolled
launch
dump | python3 tools/pick.py $PKG > $OUT/targets.txt
cat $OUT/targets.txt
head -10 $OUT/targets.txt > $OUT/.t
while read -r x y label <&3; do
  adb shell input tap $x $y; sleep 4
  if front | grep -q "$PKG"; then shot "$label" || true; fi
  launch
done 3< $OUT/.t
logcat_crash=$(adb logcat -d -b crash | tail -40); echo "$logcat_crash" > $OUT/crash.txt
rm -f $OUT/.hashes $OUT/.t
adb emu kill || true
exit 0
