import os
import subprocess
import math
import sys


def get_executable_path(exe_name):
    """
    获取可执行文件的路径。
    优先检查当前脚本所在目录，如果找不到，则假定在系统 PATH 中。
    """
    # 获取当前脚本所在的绝对目录
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # 拼接本地路径
    local_path = os.path.join(script_dir, exe_name)

    # 如果本地存在，返回本地路径；否则返回文件名（尝试系统全局调用）
    if os.path.exists(local_path):
        return local_path
    return exe_name


def split_video_by_size(input_file, target_size_mb=195):
    # 1. 确定 FFmpeg 和 FFprobe 的路径
    ffmpeg_exe = get_executable_path("ffmpeg.exe")
    ffprobe_exe = get_executable_path("ffprobe.exe")

    print(f"使用的 FFmpeg 路径: {ffmpeg_exe}")

    # 简单的检查
    if "ffmpeg.exe" in ffmpeg_exe and not os.path.exists(ffmpeg_exe) and not os.path.exists(input_file):
        # 这里的逻辑是：如果返回的是绝对路径但文件不存在，或者仅仅是文件名但系统也没装
        # 为了简化，我们主要依赖 subprocess 的报错
        pass

    if not os.path.exists(input_file):
        print(f"错误: 找不到视频文件 {input_file}")
        return

    # 2. 获取文件总大小
    file_size_mb = os.path.getsize(input_file) / (1024 * 1024)

    # 3. 获取视频时长 (使用 ffprobe)
    try:
        cmd_duration = [
            ffprobe_exe, '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', input_file
        ]
        # 注意：这里需要捕获可能的 FileNotFoundError，因为如果本地没有ffprobe且系统也没装，会报错
        output = subprocess.check_output(cmd_duration).decode('utf-8').strip()
        duration = float(output)
    except FileNotFoundError:
        print(f"错误: 找不到 {ffprobe_exe}。请确保 ffprobe.exe 也在当前目录下，或者已安装在系统中。")
        return
    except Exception as e:
        print(f"无法获取视频时长: {e}")
        return

    # 4. 计算切分参数
    num_segments = math.ceil(file_size_mb / target_size_mb)
    if num_segments <= 1:
        print(f"文件大小 ({file_size_mb:.2f}MB) 小于目标大小，无需切分。")
        return

    segment_time = duration / num_segments

    print(f"文件大小: {file_size_mb:.2f}MB, 总时长: {duration:.2f}s")
    print(f"切分方案: 共 {num_segments} 段，每段约 {segment_time:.2f}s")

    # 5. 执行切分
    output_pattern = f"{os.path.splitext(input_file)[0]}_part%03d.mp4"

    split_cmd = [
        ffmpeg_exe, '-i', input_file,
        '-c', 'copy',
        '-map', '0',
        '-segment_time', str(segment_time),
        '-f', 'segment',
        '-reset_timestamps', '1',
        output_pattern
    ]

    try:
        print("开始切分...")
        subprocess.run(split_cmd, check=True)
        print(f"成功！文件已保存为: {output_pattern}")
    except subprocess.CalledProcessError as e:
        print(f"切分出错: {e}")
    except FileNotFoundError:
        print(f"错误: 无法执行 {ffmpeg_exe}，请检查文件是否存在。")

if __name__ == "__main__":
    # 请将文件名替换为你实际的视频文件名
    # 确保视频文件、ffmpeg.exe、ffprobe.exe 和这个脚本在同一个文件夹
    video_path = r"H:\知识视频\软考架构师\第七章-数据库相关知识.mp4"  # 替换为你的视频路径
    split_video_by_size(video_path, 195)