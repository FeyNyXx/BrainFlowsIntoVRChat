import argparse
from cmath import pi
import time
import enum
import math
import numpy as np

from brainflow.board_shim import BoardShim, BrainFlowInputParams, LogLevels, BoardIds
from brainflow.data_filter import DataFilter, DetrendOperations, FilterTypes, NoiseTypes

from pythonosc.udp_client import SimpleUDPClient
from scipy.signal import find_peaks

from hexVisualThread import hexVisualThread


class BAND_POWERS(enum.IntEnum):
    Gamma = 4
    Beta = 3
    Alpha = 2
    Theta = 1
    Delta = 0


OSC_BASE_PATH = '/avatar/parameters/'


class OSC_Path:
    Relax = OSC_BASE_PATH + 'osc_relax_avg'
    Focus = OSC_BASE_PATH + 'osc_focus_avg'
    Battery = OSC_BASE_PATH + 'osc_battery_lvl'
    HeartBps = OSC_BASE_PATH + 'osc_heart_bps'
    HeartBpm = OSC_BASE_PATH + 'osc_heart_bpm'
    ConnectionStatus = OSC_BASE_PATH + 'osc_is_connected'


def tanh_normalize(data, scale, offset):
    return np.tanh(scale * (data + offset))


def smooth(current_value, target_value, weight):
    current_value = (1.0 - weight) * current_value + weight * target_value
    return current_value


def tryFunc(func, val):
    try:
        return func(val)
    except:
        return None


def main():
    BoardShim.enable_board_logger()
    DataFilter.enable_data_logger()

    ### Uncomment this to see debug messages ###
    BoardShim.set_log_level(LogLevels.LEVEL_DEBUG.value)

    ### Paramater Setting ###
    parser = argparse.ArgumentParser()
    # use docs to check which parameters are required for specific board, e.g. for Cyton - set serial port
    parser.add_argument('--timeout', type=int, help='timeout for device discovery or connection', required=False,
                        default=0)
    parser.add_argument('--ip-port', type=int,
                        help='ip port', required=False, default=0)
    parser.add_argument('--ip-protocol', type=int, help='ip protocol, check IpProtocolType enum', required=False,
                        default=0)
    parser.add_argument('--ip-address', type=str,
                        help='ip address', required=False, default='')
    parser.add_argument('--serial-port', type=str,
                        help='serial port', required=False, default='')
    parser.add_argument('--mac-address', type=str,
                        help='mac address', required=False, default='')
    parser.add_argument('--other-info', type=str,
                        help='other info', required=False, default='')
    parser.add_argument('--streamer-params', type=str,
                        help='streamer params', required=False, default='')
    parser.add_argument('--serial-number', type=str,
                        help='serial number', required=False, default='')
    parser.add_argument('--board-id', type=int, help='board id, check docs to get a list of supported boards',
                        required=True)
    parser.add_argument('--file', type=str, help='file',
                        required=False, default='')
    args = parser.parse_args()

    params = BrainFlowInputParams()
    params.ip_port = args.ip_port
    params.serial_port = args.serial_port
    params.mac_address = args.mac_address
    params.other_info = args.other_info
    params.serial_number = args.serial_number
    params.ip_address = args.ip_address
    params.ip_protocol = args.ip_protocol
    params.timeout = args.timeout
    params.file = args.file

    ### OSC Setup ###
    ip = "127.0.0.1"
    send_port = 9000
    osc_client = SimpleUDPClient(ip, send_port)

    ### Biosensor board setup ###
    board = BoardShim(args.board_id, params)
    master_board_id = board.get_board_id()
    eeg_channels = tryFunc(BoardShim.get_eeg_channels, master_board_id)
    sampling_rate = tryFunc(BoardShim.get_sampling_rate, master_board_id)
    battery_channel = tryFunc(BoardShim.get_battery_channel, master_board_id)
    ppg_channels = tryFunc(BoardShim.get_ppg_channels, master_board_id)
    time_channel = tryFunc(BoardShim.get_timestamp_channel, master_board_id)
    board.prepare_session()

    ### Biosensor device specific commands ###
    if master_board_id == BoardIds.MUSE_2_BOARD or master_board_id == BoardIds.MUSE_S_BOARD:
        board.config_board('p52')

    ### EEG Band Calculation Params ###
    current_focus = 0
    current_relax = 0
    current_value = np.array([current_focus, current_relax])
    eeg_window_size = 2

    # normalize ratios between -1 and 1.
    # Ratios are centered around 1.0. Tune scale to taste
    normalize_offset = -1
    normalize_scale = 1.3

    # Smoothing params
    smoothing_weight = 0.05
    detrend_eeg = True

    ### PPG Params ###
    heart_window_size = 10
    heart_min_dist = 0.35
    heart_lowpass_cutoff = 2
    heart_lowpass_order = 2
    heart_lowpass_ripple = 0

    ### Streaming Params ###
    update_speed = 1 / 4  # 4Hz update rate for VRChat OSC
    ring_buffer_size = max(eeg_window_size, heart_window_size) * sampling_rate
    startup_time = 5
    board_timeout = 5

    ### Visualizer thread ###
    t = hexVisualThread()

    try:
        # Start Visualizer on a different thread
        t.start()

        BoardShim.log_message(LogLevels.LEVEL_INFO.value, 'Intializing')
        board.start_stream(ring_buffer_size, args.streamer_params)
        time.sleep(startup_time)

        BoardShim.log_message(LogLevels.LEVEL_INFO.value, 'Main Loop Started')
        while True:
            BoardShim.log_message(
                LogLevels.LEVEL_DEBUG.value, "Getting Board Data")
            data = board.get_current_board_data(
                eeg_window_size * sampling_rate)

            BoardShim.log_message(LogLevels.LEVEL_DEBUG.value, "Timeout Check")
            time_data = data[time_channel]
            last_sample_time = time_data[-1]
            current_time = time.time()
            if current_time - last_sample_time > board_timeout:
                raise TimeoutError("Biosensor board timed out")

            battery_level = None if not battery_channel else data[battery_channel][-1]
            if battery_level:
                BoardShim.log_message(
                    LogLevels.LEVEL_DEBUG.value, "Battery: {}".format(battery_level))

            ### START EEG SECTION ###
            BoardShim.log_message(
                LogLevels.LEVEL_DEBUG.value, "Calculating Power Bands")

            # Clean Signals
            for eeg_channel in eeg_channels:
                DataFilter.remove_environmental_noise(data[eeg_channel],
                                                      BoardShim.get_sampling_rate(master_board_id), NoiseTypes.FIFTY_AND_SIXTY.value)
                if detrend_eeg:
                    DataFilter.detrend(data[eeg_channel],
                                       DetrendOperations.LINEAR)

            bands = DataFilter.get_avg_band_powers(
                data, eeg_channels, sampling_rate, True)
            feature_vector, _ = bands

            BoardShim.log_message(
                LogLevels.LEVEL_DEBUG.value, "Calculating Metrics")
            numerator = np.array(
                [feature_vector[BAND_POWERS.Beta], feature_vector[BAND_POWERS.Alpha]])
            denominator = np.array(
                [feature_vector[BAND_POWERS.Theta], feature_vector[BAND_POWERS.Theta]])
            target_value = np.divide(numerator, denominator)
            target_value = tanh_normalize(
                target_value, normalize_scale, normalize_offset)
            current_value = smooth(
                current_value, target_value, smoothing_weight)

            current_focus = current_value[0]
            current_relax = current_value[1]

            # Update Visuals
            visual_alpha = (current_focus + 1)/2
            t.update_alpha(visual_alpha * visual_alpha)

            BoardShim.log_message(LogLevels.LEVEL_DEBUG.value, "Focus: {:.3f}\tRelax: {:.3f}".format(
                current_focus, current_relax))

            normalized_feature_vector = tanh_normalize(
                feature_vector, 12, -0.5) / math.pi + 0.5

            ### END EEG SECTION ###

            ### START PPG SECTION ###
            if ppg_channels and time_channel:
                BoardShim.log_message(
                    LogLevels.LEVEL_DEBUG.value, "Get PPG Data")
                data = board.get_current_board_data(
                    heart_window_size * sampling_rate)
                time_data = data[time_channel]
                ir_data_channel = ppg_channels[1]
                ambient_channel = ppg_channels[0]

                BoardShim.log_message(
                    LogLevels.LEVEL_DEBUG.value, "Clean PPG Signals")
                ir_data = data[ir_data_channel] - data[ambient_channel]
                ambient_filter = list(map(lambda sample: sample > 0, ir_data))
                ir_data = ir_data[ambient_filter]
                DataFilter.perform_lowpass(
                    ir_data, sampling_rate, heart_lowpass_cutoff, heart_lowpass_order, FilterTypes.BUTTERWORTH.value, heart_lowpass_ripple)

                BoardShim.log_message(
                    LogLevels.LEVEL_DEBUG.value, "Find PPG Peaks")
                peaks, _ = find_peaks(
                    ir_data, distance=sampling_rate * heart_min_dist)
                peaks = peaks[1:-1]

                BoardShim.log_message(
                    LogLevels.LEVEL_DEBUG.value, "Calculate Heart Rate")
                heart_bps = 1 / np.mean(np.diff(time_data[peaks]))
                if not math.isnan(heart_bps):
                    heart_bpm = int(heart_bps * 60 + 0.5)
                    BoardShim.log_message(
                        LogLevels.LEVEL_DEBUG.value, "BPS: {:.3f}\tBPM: {}".format(heart_bps, heart_bpm))
                else:
                    heart_bps = None
            ### END PPG SECTION ###

            BoardShim.log_message(LogLevels.LEVEL_DEBUG.value, "Sending")
            osc_client.send_message(OSC_Path.Focus, current_focus)
            osc_client.send_message(OSC_Path.Relax, current_relax)
            osc_client.send_message(OSC_Path.ConnectionStatus, True)
            if battery_level:
                osc_client.send_message(OSC_Path.Battery, battery_level)
            if ppg_channels and heart_bps:
                osc_client.send_message(OSC_Path.HeartBps, heart_bps)
                osc_client.send_message(OSC_Path.HeartBpm, heart_bpm)

            for band_power in BAND_POWERS:
                osc_path = OSC_BASE_PATH + "osc_band_power_" + band_power.name.lower()
                band_value = feature_vector[band_power.value]
                osc_client.send_message(osc_path, band_value)

            BoardShim.log_message(LogLevels.LEVEL_DEBUG.value, "Sleeping")
            time.sleep(update_speed)

    except KeyboardInterrupt:
        BoardShim.log_message(LogLevels.LEVEL_INFO.value, 'Shutting down')
    except TimeoutError:
        BoardShim.log_message(LogLevels.LEVEL_INFO.value,
                              'Biosensor board timed out')
    finally:
        osc_client.send_message(OSC_Path.ConnectionStatus, False)
        ### Cleanup ###
        board.stop_stream()
        board.release_session()


if __name__ == "__main__":
    main()
