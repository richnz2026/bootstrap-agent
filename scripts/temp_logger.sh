#!/bin/bash
LOGFILE=~/temp_history.log
while true; do
    GPU=$(nvidia-smi --query-gpu=timestamp,temperature.gpu,power.draw,utilization.gpu,fan.speed,clocks_throttle_reasons.active,memory.used --format=csv,noheader)
    CPU=$(top -bn1 | grep 'Cpu(s)' | awk '{printf "%.1f", $2+$4}')
    RAM=$(free -b | awk '/^Mem:/{printf "%.1f", $3/$2*100}')
    echo "$GPU, $CPU, $RAM" >> $LOGFILE
    sleep 30
done
