import sys
import subprocess
import threading
import time
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import Qt

# 尝试导入 pyaudiowpatch
import pyaudiowpatch as pyaudio

"""
2026-02-01 调试失败
"""

# ==========================================
# 1. 区域选择 UI (保持不变)
# ==========================================
class SelectionOverlay(QtWidgets.QWidget):
    selection_made = QtCore.pyqtSignal(int, int, int, int)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.start_point = None
        self.end_point = None
        self.setGeometry(QtGui.QGuiApplication.primaryScreen().virtualGeometry())

    def mousePressEvent(self, event):
        self.start_point = event.pos()
        self.end_point = event.pos()
        self.update()

    def mouseMoveEvent(self, event):
        self.end_point = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        rect = self.get_normalized_rect()
        if rect.width() > 10 and rect.height() > 10:
            self.selection_made.emit(rect.x(), rect.y(), rect.width(), rect.height())
            self.close()
        else:
            self.start_point = None
            self.end_point = None
            self.update()

    def get_normalized_rect(self):
        if not self.start_point or not self.end_point:
            return QtCore.QRect()
        return QtCore.QRect(self.start_point, self.end_point).normalized()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setBrush(QtGui.QColor(0, 0, 0, 150))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())

        if self.start_point and self.end_point:
            selection_rect = self.get_normalized_rect()
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
            painter.setBrush(QtGui.QColor(0, 0, 0, 0))
            painter.drawRect(selection_rect)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QtGui.QPen(QtGui.QColor(0, 255, 255), 2)
            pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selection_rect)


# ==========================================
# 2. 录制逻辑 (增强健壮性)
# ==========================================
class RecorderWorker(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(str)
    error_signal = QtCore.pyqtSignal(str)

    def __init__(self, rect, filename):
        super().__init__()
        self.rect = rect
        self.filename = filename
        self.is_recording = False
        self.ffmpeg_process = None
        self.pa = None
        self.stream = None

    def run(self):
        # if 'pyaudio' not in sys.modules:
        #     self.error_signal.emit("缺少库: 请先运行 pip install pyaudiowpatch")
        #     return

        self.is_recording = True
        error_msg = ""

        try:
            # --- 1. 音频设备初始化 ---
            self.pa = pyaudio.PyAudio()
            try:
                # 获取默认的 WASAPI Loopback 设备
                wasapi_info = self.pa.get_default_wasapi_loopback()
            except OSError:
                raise Exception("无法初始化系统音频捕获。\n建议：请先播放一段音乐(让声卡激活)，然后再点击录制。")

            # 关键：严格匹配设备的采样率和声道
            input_device_index = wasapi_info["index"]
            samplerate = int(wasapi_info["defaultSampleRate"])
            channels = int(wasapi_info["maxInputChannels"])

            # print(f"[Debug] 音频源: {wasapi_info['name']} | SR: {samplerate} | CH: {channels}")

            # --- 2. 启动 FFmpeg ---
            x, y, w, h = self.rect
            # 宽高必须是偶数
            w = w if w % 2 == 0 else w - 1
            h = h if h % 2 == 0 else h - 1

            cmd = [
                'ffmpeg', '-y',
                '-f', 'gdigrab', '-framerate', '30',
                '-offset_x', str(x), '-offset_y', str(y), '-video_size', f"{w}x{h}",
                '-i', 'desktop',  # 视频流
                '-f', 's16le',  # 音频格式 (对应 paInt16)
                '-ac', str(channels),
                '-ar', str(samplerate),
                '-i', '-',  # 音频流来自标准输入 (Pipe)
                '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                self.filename
            ]

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # 隐藏黑框

            self.ffmpeg_process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,  # 捕获错误输出
                startupinfo=startupinfo
            )

            # --- 3. 启动音频流并循环写入 ---
            def audio_callback(in_data, frame_count, time_info, status):
                # 这个回调在音频线程运行，直接把数据推入一个 buffer 或者简单处理
                # 但为了简单，我们使用 blocking mode (非 callback) 在下面的 while 循环里读
                return (in_data, pyaudio.paContinue)

            self.stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=samplerate,
                input=True,
                input_device_index=input_device_index,
                frames_per_buffer=1024
            )

            # 循环读取音频并写入 FFmpeg
            while self.is_recording:
                # 检查 FFmpeg 是否意外退出
                if self.ffmpeg_process.poll() is not None:
                    # 读取错误信息
                    stderr_output = self.ffmpeg_process.stderr.read().decode('mbcs', errors='replace')
                    raise Exception(f"FFmpeg 意外退出:\n{stderr_output}")

                try:
                    # 读取音频数据 (阻塞式)
                    data = self.stream.read(1024)
                    # 写入 FFmpeg 管道
                    self.ffmpeg_process.stdin.write(data)
                except Exception as e:
                    # 写入管道失败通常意味着 FFmpeg 已经关了
                    break

        except Exception as e:
            error_msg = str(e)
        finally:
            self.cleanup()

            if error_msg:
                self.error_signal.emit(error_msg)
            else:
                self.finished_signal.emit(f"录制成功！\n文件已保存至:\n{self.filename}")

    def stop_recording(self):
        self.is_recording = False

    def cleanup(self):
        # 1. 停止 PyAudio
        if self.stream:
            try:
                self.stream.stop_stream()
                self.stream.close()
            except:
                pass
            self.stream = None
        if self.pa:
            try:
                self.pa.terminate()
            except:
                pass
            self.pa = None

        # 2. 优雅关闭 FFmpeg (发送 EOF)
        if self.ffmpeg_process:
            try:
                if self.ffmpeg_process.poll() is None:
                    self.ffmpeg_process.stdin.close()  # 关键：关闭输入流，告诉 FFmpeg 录制结束
                    self.ffmpeg_process.wait(timeout=5)  # 等待封装文件
            except:
                self.ffmpeg_process.kill()
            self.ffmpeg_process = None


# ==========================================
# 3. 主界面 (增加文件选择)
# ==========================================
class ScreenRecorderApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python 录屏专家 (系统内录版)")
        self.resize(500, 300)
        self.recording_area = None
        self.recorder_thread = None
        self.save_path = ""  # 保存路径
        self.init_ui()

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout()
        layout.setSpacing(15)

        # --- 区域选择 ---
        self.lbl_area = QtWidgets.QLabel("1. 录制区域: 全屏 (默认)")
        self.lbl_area.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.lbl_area)

        self.btn_select_area = QtWidgets.QPushButton("📐 框选区域")
        self.btn_select_area.clicked.connect(self.start_selection)
        layout.addWidget(self.btn_select_area)

        layout.addSpacing(10)

        # --- 文件保存 ---
        layout.addWidget(QtWidgets.QLabel("2. 保存位置:"))
        file_layout = QtWidgets.QHBoxLayout()
        self.line_edit_path = QtWidgets.QLineEdit()
        self.line_edit_path.setPlaceholderText("请选择保存路径...")
        self.line_edit_path.setReadOnly(True)
        file_layout.addWidget(self.line_edit_path)

        self.btn_browse = QtWidgets.QPushButton("📂 浏览...")
        self.btn_browse.clicked.connect(self.choose_file)
        file_layout.addWidget(self.btn_browse)
        layout.addLayout(file_layout)

        layout.addSpacing(10)

        # --- 控制区 ---
        self.lbl_status = QtWidgets.QLabel("就绪 - 等待开始")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_status.setStyleSheet("color: gray;")
        layout.addWidget(self.lbl_status)

        self.btn_record = QtWidgets.QPushButton("🔴 开始录制")
        self.btn_record.setFixedHeight(50)
        self.btn_record.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.btn_record.clicked.connect(self.toggle_recording)
        layout.addWidget(self.btn_record)

        self.setLayout(layout)

    def choose_file(self):
        # 打开文件保存对话框
        file_name, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "保存录屏文件",
            "MyRecord.mp4",
            "Video Files (*.mp4)"
        )
        if file_name:
            self.save_path = file_name
            self.line_edit_path.setText(self.save_path)

    def start_selection(self):
        self.hide()
        self.overlay = SelectionOverlay()
        self.overlay.selection_made.connect(self.on_selection_made)
        self.overlay.show()

    def on_selection_made(self, x, y, w, h):
        self.recording_area = (x, y, w, h)
        self.lbl_area.setText(f"1. 录制区域: {w}x{h} (X:{x}, Y:{y})")
        self.show()

    def toggle_recording(self):
        if self.recorder_thread and self.recorder_thread.isRunning():
            # 停止
            self.lbl_status.setText("正在封装视频，请稍候...")
            self.btn_record.setEnabled(False)
            self.recorder_thread.stop_recording()
        else:
            # 开始前的检查
            if not self.save_path:
                QtWidgets.QMessageBox.warning(self, "提示", "请先选择保存文件的位置！")
                self.choose_file()
                if not self.save_path: return

            if not self.recording_area:
                screen = QtGui.QGuiApplication.primaryScreen().geometry()
                self.recording_area = (0, 0, screen.width(), screen.height())

            self.recorder_thread = RecorderWorker(self.recording_area, self.save_path)
            self.recorder_thread.finished_signal.connect(self.on_recording_finished)
            self.recorder_thread.error_signal.connect(self.on_recording_error)

            self.recorder_thread.start()

            self.btn_record.setText("⏹ 停止录制")
            self.btn_record.setStyleSheet("background-color: #ffcccc; color: red;")
            self.lbl_status.setText("🔴 录制中 (由系统音频驱动)...")

    def on_recording_finished(self, msg):
        self.btn_record.setText("🔴 开始录制")
        self.btn_record.setStyleSheet("")
        self.btn_record.setEnabled(True)
        self.lbl_status.setText("录制完成")
        QtWidgets.QMessageBox.information(self, "完成", msg)

    def on_recording_error(self, err_msg):
        self.btn_record.setText("🔴 开始录制")
        self.btn_record.setStyleSheet("")
        self.btn_record.setEnabled(True)
        self.lbl_status.setText("发生错误")
        QtWidgets.QMessageBox.critical(self, "录制失败", f"{err_msg}")


if __name__ == "__main__":
    # 高分屏适配
    if hasattr(QtCore.Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

    app = QtWidgets.QApplication(sys.argv)
    window = ScreenRecorderApp()
    window.show()
    sys.exit(app.exec())