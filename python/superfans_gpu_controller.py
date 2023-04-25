# Superfans GPU controller
#
# author: Domen Tabernik
# 2019

import time
import superfans
import subprocess
import signal
import sys
import json
import psutil


class GracefulKiller:
    kill_now = False

    def __init__(self):
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self,signum, frame):
        self.kill_now = True


def enable_persistance_nvidia():
    cmd = 'nvidia-smi -pm 1'
    s = subprocess.check_output(cmd + " 2>&1", shell=True)


def retrieve_nvidia_gpu_temperature():
    cmd = 'nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader'

    s = subprocess.check_output(cmd + " 2>&1", shell=True)
    if len(s) <= 0:
        return False
    s = s.decode('ascii')

    out = [int(x.strip()) for x in s.split("\n") if len(x.strip()) > 0]
    if out:
        return out
    else:
        return False


def retrieve_cpu_temperature():
    temps = psutil.sensors_temperatures()
    if not temps:
        print("No temperature sensors found")
        return

    max_temp = 0
    for name, entries in temps.items():
        if name.startswith("coretemp") or name.startswith("cpu_thermal") or name.startswith("k10temp"):
            for entry in entries:
                if entry.label.startswith("Package id") or entry.label.startswith("Tdie") or entry.label == "CPU":
                    max_temp = max(max_temp, entry.current)

    return max_temp


def superfans_gpu_controller(fan_settings, FAN_DECREASE_MIN_TIME=30, sleep_sec=2, gpu_moving_avg_num=5, fan_target_eps=2.0):
    """
    Controller function that monitors GPU temperature in constant loop and adjusts FAN speeds based on provided `fan_settings`.
    After the loop the default preset is restored.

    :param fan_settings: dictionary that maps the temperature in deg C to % of fan speed
    :param FAN_DECREASE_MIN_TIME: minimal time before a fan speed is again reduced (based on previous change) default=120
    :param sleep_sec: loop sleep time (default=2 sec)
    :param gpu_moving_avg_num: moving average for GPU i.e. the number of last measurements that are averaged (default=5)
    :param fan_target_eps: tolerance of fan target w.r.t. the the actual value in deg C (default=1.0)
    :return:
    """
    superfan_config = dict(hostname='localhost')

    # convert fan_settings keys from string to int
    fan_settings = { int(k): fan_settings[k] for k in sorted(fan_settings.keys()) }

    # save default present before changing anything
    default_preset = superfans.get_preset(superfan_config)
    print('Started fan control using GPU temperature.')
    print('Using settings:')
    for k in sorted(fan_settings.keys()):
        print('\t%d C = %d ' % (k, fan_settings[k]) + "%")
    print('\n')

    # put GPUs into persistance mode so that nvidia-smi will retern immediately
    enable_persistance_nvidia()

    try:
        ZONES = [
            superfans.FAN_ZONE_SYS1,
            superfans.FAN_ZONE_SYS2,
            superfans.FAN_ZONE_SYS3,
            superfans.FAN_ZONE_SYS4,
        ]
        FAN_MEMBERS = superfans.FAN_ZONES_MEMBERS[superfans.FAN_ZONE_SYS1] + \
                      superfans.FAN_ZONES_MEMBERS[superfans.FAN_ZONE_SYS2] + \
                      superfans.FAN_ZONES_MEMBERS[superfans.FAN_ZONE_SYS3] + \
                      superfans.FAN_ZONES_MEMBERS[superfans.FAN_ZONE_SYS4]

        # GPU moving average
        previous_target_fan = None
        previous_update_time = None

        prev_GPU_temp = []
        #last_GPU_temp = None

        # ensure correct ending when SIGINT and SIGTERM are received
        k = GracefulKiller()
        while not k.kill_now:

            # get GPU, CPU temperature
            GPU_temp = retrieve_nvidia_gpu_temperature()
            max_cpu_temp = retrieve_cpu_temperature()

            #prev_GPU_temp.append(GPU_temp)
            #last_GPU_temp = GPU_temp

            # # continue until we have enough sampels for moving average
            # if len(prev_GPU_temp) < gpu_moving_avg_num:
            #     continue
            #
            # # retain last 5 mesurements
            # prev_GPU_temp = prev_GPU_temp[-gpu_moving_avg_num:]
            # mean_GPU_temp = prev_GPU_temp[0]
            # for gpu_temp in prev_GPU_temp[1:]:
            #     mean_GPU_temp = [x+y for x, y in zip(gpu_temp, mean_GPU_temp)]
            #
            # mean_GPU_temp = [x/len(prev_GPU_temp) for x in mean_GPU_temp]
            mean_GPU_temp = [x * 0.5 + y * 0.5 for x, y in zip(GPU_temp, mean_GPU_temp)]

            max_gpu_temp = max(mean_GPU_temp)
            max_temp = max(max_gpu_temp, max_cpu_temp)

            for key_temp in sorted(fan_settings.keys())[::-1]:
                if key_temp <= max_temp:
                    target_fan = fan_settings[key_temp]
                    break

            # This is slow if the CPU is busy
            current_fan_levels = superfans.get_fan(superfan_config, FAN_MEMBERS)
            current_update_time = time.time()
            diff_sys_fan = [abs(current_fan_levels[FAN] - target_fan) for FAN in FAN_MEMBERS if FAN in current_fan_levels and current_fan_levels[FAN] > 0]

            # TODO: ignore outlier FANs in case they are faulty

            disable_update = False

            if previous_update_time is not None and previous_target_fan is not None:
                has_enough_time_elapsed = current_update_time - previous_update_time > FAN_DECREASE_MIN_TIME
                is_level_down_change = target_fan < previous_target_fan
                disable_update = True if is_level_down_change and not has_enough_time_elapsed else False

            if not disable_update:
                # Allow for 1% difference in target
                update_sys_fan = any([d > fan_target_eps for d in diff_sys_fan])
                if update_sys_fan:
                    for z in ZONES:
                        superfans.set_fan(superfan_config, target_fan, z)

                print("update sys %s " % update_sys_fan)
                print('\tCurrent GPU measurements (in C): %s' % ','.join(map(str, GPU_temp)))
                print('\tCurrent CPU measurements (in C): %s' % max_cpu_temp)
                print('\tMoving average GPU measurements (in C): %s  (max=%d)' % (','.join(map(str, mean_GPU_temp)), max_gpu_temp))
                print('\tTarget FAN speed: %d C => FAN %d %% (difference:  SYS1,2,3,4 fan = %.2f)' % (max_temp, target_fan, max(diff_sys_fan)))
                print('\n\n')

                if update_sys_fan:
                    previous_target_fan = target_fan
                    previous_update_time = current_update_time

            time.sleep(sleep_sec)
    finally:
        # revert back to default preset before finishing
        superfans.set_preset(superfan_config, default_preset)
        print('Reverted back to system default.')


def main():
    if len(sys.argv) != 2:
        print('Invalid number of arguments: missing configuration file!!')
        print('')
        print(' Usage: %s [PATH_TO_JSON_CONFIG]' % sys.argv[0])
        print('')
        print(' Configuration file in JSON format should include "fan_settings" = {[in deg C]: [% fan], ...} ')
        print
        exit(0)

    with open(sys.argv[1]) as cfg_file:
        cfg = json.load(cfg_file)

    superfans_gpu_controller(cfg['fan_settings'])


if __name__  == "__main__":
    main()
