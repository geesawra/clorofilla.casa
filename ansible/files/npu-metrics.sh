#!/bin/bash
# Write NPU metrics in Prometheus textfile format for node_exporter.
# Run as root (debugfs requires it). Meant to be called from cron.

DBG=/sys/kernel/debug/rknpu
OUT=/var/lib/node_exporter/textfile_collector/npu.prom
TMP="${OUT}.$$"

mkdir -p "$(dirname "$OUT")"

if [ ! -d "${DBG}" ]; then
    exit 0  # rknpu not loaded, nothing to export
fi

# Read NPU debugfs
version=$(cat "${DBG}/version" 2>/dev/null)
freq=$(cat "${DBG}/freq" 2>/dev/null)
volt=$(cat "${DBG}/volt" 2>/dev/null)
power_raw=$(cat "${DBG}/power" 2>/dev/null)
load=$(cat "${DBG}/load" 2>/dev/null)

# Parse power: "OFF" or value with suffix. Only numeric values become metrics.
if [ "$power_raw" != "OFF" ] && [ -n "$power_raw" ]; then
    power_num=$(echo "$power_raw" | grep -oE '[0-9]+')
    if [ -n "$power_num" ]; then
        echo "rknpu_power_usage_mw ${power_num}" >> "$TMP"
    fi
fi

# Core metrics
[ -n "$freq" ] && echo "rknpu_freq_hz ${freq}" >> "$TMP"
[ -n "$volt" ] && echo "rknpu_voltage_uv ${volt}" >> "$TMP"

# Parse load: "NPU load:  Core0: 28%, Core1:  0%, Core2:  0%,"
if [ -n "$load" ]; then
    echo "$load" | grep -oE 'Core[0-9]+: *[0-9]+%' | while read -r core; do
        name=$(echo "$core" | grep -oE 'Core[0-9]+' | tr '[:upper:]' '[:lower:]')
        pct=$(echo "$core" | grep -oE '[0-9]+%' | tr -d '%')
        echo "rknpu_load_percent{core=\"${name}\"} ${pct}" >> "$TMP"
    done
fi

# NPU interrupts (columns: virq cpu0..cpu7 hwirq trigger device)
awk '/fdab0000.npu/ {
    irq = $1
    gsub(/:$/, "", irq)
    printf "rknpu_interrupts_total{irq=\"%s\",cpu=\"total\"} %d\n", irq, $2+$3+$4+$5+$6+$7+$8+$9
    for (i = 2; i <= 9; i++)
        printf "rknpu_interrupts_total{irq=\"%s\",cpu=\"cpu%d\"} %d\n", irq, i-2, $i
}' /proc/interrupts >> "$TMP"

# Metadata
echo "rknpu_info{version=\"${version}\"} 1" >> "$TMP"

mv "$TMP" "$OUT"
