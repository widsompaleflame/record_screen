import sys
import subprocess
import re
from PyQt6 import QtWidgets, QtCore, QtGui
from PyQt6.QtCore import Qt
import pyaudiowpatch as pyaudio


# ==========================================
# 1. 区域选择窗口 (透明遮罩层)
# ==========================================
class SelectionOverlay(QtWidgets.QWidget):
    # 定义一个信号，当选择完成时发送 (x, y, w, h)
    selection_made = QtCore.pyqtSignal(int, int, int, int)

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        # 背景透明 (具体的绘制在 paintEvent 里完成)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.CrossCursor)

        self.start_point = None
        self.end_point = None

        # 覆盖全屏
        self.setGeometry(QtGui.QGuiApplication.primaryScreen().virtualGeometry())

    def mousePressEvent(self, event):
        self.start_point = event.pos()
        self.end_point = event.pos()
        self.update()

    def mouseMoveEvent(self, event):
        self.end_point = event.pos()
        self.update()

    def mouseReleaseEvent(self, event):
        # 计算选区
        rect = QtCore.QRect(self.start_point, self.end_point).normalized()
        if rect.width() > 10 and rect.height() > 10:
            # 发送全局坐标
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

        # 1. 绘制全屏半透明黑色遮罩 (更明显的黑色: Alpha=150)
        painter.setBrush(QtGui.QColor(0, 0, 0, 150))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(self.rect())

        # 2. 如果有选区，从遮罩中“挖空”这一块
        if self.start_point and self.end_point:
            selection_rect = self.get_normalized_rect()

            # 设置混合模式为 Clear (相当于橡皮擦，把像素变透明)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_Clear)
            painter.setBrush(QtGui.QColor(0, 0, 0, 0))  # 颜色不重要，关键是模式
            painter.drawRect(selection_rect)

            # 3. 恢复正常混合模式，给选区画一个显眼的边框
            painter.setCompositionMode(QtGui.QPainter.CompositionMode.CompositionMode_SourceOver)
            pen = QtGui.QPen(QtGui.QColor(0, 255, 255), 2)  # 青色边框
            pen.setStyle(Qt.PenStyle.DashLine)  # 虚线
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(selection_rect)


# ==========================================
# 2. 录制工作线程 (防止阻塞界面)
# ==========================================
class RecorderWorker(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(str)  # 发送结束消息
    error_signal = QtCore.pyqtSignal(str)

    def __init__(self, rect, audio_device, filename="output.mp4"):
        super().__init__()
        self.rect = rect  # tuple (x, y, w, h)
        self.audio_device = audio_device
        self.filename = filename
        self.process = None
        self.is_recording = False

    def run(self):
        x, y, w, h = self.rect

        # 确保宽高是偶数 (FFmpeg x264 编码要求)
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1

        # 构建 FFmpeg 命令
        # -f gdigrab: Windows 屏幕捕获
        # -offset_x/y -video_size: 区域选择
        # -f dshow: DirectShow 音频捕获
        cmd = [
            'ffmpeg',
            '-y',  # 覆盖输出文件
            '-f', 'gdigrab',
            '-framerate', '30',
            '-offset_x', str(x),
            '-offset_y', str(y),
            '-video_size', f"{w}x{h}",
            '-i', 'desktop',  # 输入源：桌面
        ]

        # 如果选择了音频设备，则添加音频参数
        if self.audio_device and self.audio_device != "无 (仅录屏)":
            # 关键：加上 buffer 选项防止音频溢出
            cmd.extend([
                '-f', 'dshow',
                '-i', f'audio={self.audio_device}'
            ])

        # 编码参数
        cmd.extend([
            '-c:v', 'libx264',
            '-preset', 'ultrafast',  # 极速模式，降低CPU占用
            '-pix_fmt', 'yuv420p',
            '-c:a', 'aac',
            self.filename
        ])

        print("执行命令:", " ".join(cmd))

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # 隐藏 FFmpeg 窗口

            # 启动 FFmpeg 进程
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,  # 允许我们发送 'q' 来停止
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                startupinfo=startupinfo
            )
            self.is_recording = True

            # 实时读取 stderr 以便调试错误 (FFmpeg 日志都在 stderr)
            # 等待进程结束
            self.process.wait()
            self.finished_signal.emit(f"录制完成: {self.filename}")

        except Exception as e:
            self.error_signal.emit(str(e))
        finally:
            self.is_recording = False

    def stop_recording(self):
        if self.process and self.is_recording:
            # 向 FFmpeg 发送 'q' 字符以优雅停止录制 (避免文件损坏)
            try:
                self.process.communicate(input=b'q')
            except:
                self.process.kill()


# ==========================================
# 3. 主界面
# ==========================================
class ScreenRecorderApp(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Python 专业录屏工具")
        self.resize(450, 300)

        self.recording_area = None  # (x, y, w, h)
        self.recorder_thread = None

        self.init_ui()
        # 延时加载设备，防止界面启动卡顿
        QtCore.QTimer.singleShot(100, self.load_audio_devices)

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout()
        layout.setSpacing(15)

        # 区域选择显示
        self.lbl_area = QtWidgets.QLabel("录制区域: 全屏 (默认)")
        self.lbl_area.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.lbl_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_area)

        # 区域选择按钮
        btn_layout = QtWidgets.QHBoxLayout()
        self.btn_select_area = QtWidgets.QPushButton("📐 框选区域")
        self.btn_select_area.setMinimumHeight(40)
        self.btn_select_area.clicked.connect(self.start_selection)
        btn_layout.addWidget(self.btn_select_area)
        layout.addLayout(btn_layout)

        # 音频设备选择
        group_audio = QtWidgets.QGroupBox("音频设置")
        audio_layout = QtWidgets.QVBoxLayout()

        self.combo_audio = QtWidgets.QComboBox()
        self.combo_audio.addItem("无 (仅录屏)")
        audio_layout.addWidget(self.combo_audio)

        # 添加提示链接
        self.lbl_audio_hint = QtWidgets.QLabel("⚠️ 录制系统声音需要启用【立体声混音】")
        self.lbl_audio_hint.setStyleSheet("color: #d9534f; font-size: 11px;")
        self.lbl_audio_hint.setOpenExternalLinks(True)
        audio_layout.addWidget(self.lbl_audio_hint)

        group_audio.setLayout(audio_layout)
        layout.addWidget(group_audio)

        # 状态指示
        self.lbl_status = QtWidgets.QLabel("就绪")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_status)

        # 开始/停止按钮
        self.btn_record = QtWidgets.QPushButton("🔴 开始录制")
        self.btn_record.setFixedHeight(50)
        self.btn_record.setStyleSheet("""
                    QPushButton { background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 5px; font-size: 16px; }
                    QPushButton:hover { background-color: #e0e0e0; }
                """)
        self.btn_record.clicked.connect(self.toggle_recording)
        layout.addWidget(self.btn_record)

        self.setLayout(layout)

    def load_audio_devices(self):
        """使用 ffmpeg -list_devices true -f dshow -i dummy 来获取设备列表"""
        self.lbl_status.setText("正在扫描音频设备...")
        self.combo_audio.clear()
        self.combo_audio.addItem("无 (仅录屏)")

        try:
            # 这里的 encoding='mbcs' 是解决 Windows 中文乱码的关键
            # mbcs 会根据系统当前的 ANSI 代码页 (如 GBK) 解码
            cmd = ['ffmpeg', '-list_devices', 'true', '-f', 'dshow', '-i', 'dummy']
            # FFmpeg 输出设备信息在 stderr 中
            result = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8',
                                    errors='replace')
            output = result.stderr

            # 简单的正则匹配音频设备名称
            # 输出格式通常是: [dshow @ ...]  "设备名"
            # [dshow @ ...]     Alternative name "@device_cm_{...}"

            lines = output.split('\n')
            is_audio_section = False
            devices = []

            for line in lines:
                if "DirectShow audio devices" in line:
                    is_audio_section = True
                    continue
                if "DirectShow video devices" in line:
                    is_audio_section = False
                    continue

                if is_audio_section:
                    # 匹配双引号中的设备名
                    match = re.search(r'\"(.+?)\"', line)
                    if match:
                        dev_name = match.group(1)
                        # 排除掉一些奇怪的设备ID行
                        if not dev_name.startswith("@device_"):
                            devices.append(dev_name)

            # 去重
            devices = sorted(list(set(devices)))

            has_stereo_mix = False
            for dev in devices:
                self.combo_audio.addItem(dev)
                if "立体声混音" in dev or "Stereo Mix" in dev:
                    has_stereo_mix = True

            if devices:
                self.lbl_status.setText(f"发现 {len(devices)} 个音频设备")
            else:
                self.lbl_status.setText("未发现音频设备")

            # 智能提示
            if has_stereo_mix:
                self.lbl_audio_hint.setText("✅ 检测到立体声混音，选择它即可录制系统音")
                self.lbl_audio_hint.setStyleSheet("color: green; font-weight: bold;")
                # 自动选中立体声混音
                idx = self.combo_audio.findText("立体声混音")
                if idx == -1: idx = self.combo_audio.findText("Stereo Mix")
                if idx != -1: self.combo_audio.setCurrentIndex(idx)
            else:
                self.lbl_audio_hint.setText("❌ 未检测到【立体声混音】，无法录制系统内部声音")

        except FileNotFoundError:
            QtWidgets.QMessageBox.critical(self, "错误", "未找到 ffmpeg.exe。请确保它已安装并在系统路径中。")
            self.lbl_status.setText("错误: 缺少 FFmpeg")

    def start_selection(self):
        # 隐藏主窗口，显示选择遮罩
        self.hide()
        self.overlay = SelectionOverlay()
        self.overlay.selection_made.connect(self.on_selection_made)
        self.overlay.show()

    def on_selection_made(self, x, y, w, h):
        self.recording_area = (x, y, w, h)
        self.lbl_area.setText(f"录制区域: X={x}, Y={y}, {w}x{h}")
        self.show()  # 显示主窗口

    def toggle_recording(self):
        if self.recorder_thread and self.recorder_thread.isRunning():
            # 停止录制
            self.lbl_status.setText("正在停止...")
            self.btn_record.setEnabled(False)
            self.recorder_thread.stop_recording()
        else:
            # 开始录制
            if not self.recording_area:
                # 如果没选区域，默认全屏 (获取主屏分辨率)
                screen = QtGui.QGuiApplication.primaryScreen().geometry()
                self.recording_area = (0, 0, screen.width(), screen.height())

            audio_dev = self.combo_audio.currentText()

            self.recorder_thread = RecorderWorker(self.recording_area, audio_dev)
            self.recorder_thread.finished_signal.connect(self.on_recording_finished)
            self.recorder_thread.error_signal.connect(self.on_recording_error)

            self.recorder_thread.start()

            self.btn_record.setText("⏹ 停止录制")
            self.btn_record.setStyleSheet("background-color: #ffcccc; color: red;")
            self.lbl_status.setText("🔴 录制中...")

    def on_recording_finished(self, msg):
        self.btn_record.setText("🔴 开始录制")
        self.btn_record.setStyleSheet("""
                    QPushButton { background-color: #f0f0f0; border: 1px solid #ccc; border-radius: 5px; font-size: 16px; }
                    QPushButton:hover { background-color: #e0e0e0; }
                """)
        self.btn_record.setEnabled(True)
        self.lbl_status.setText("录制完成")
        QtWidgets.QMessageBox.information(self, "成功", msg)

    def on_recording_error(self, err_msg):
        self.btn_record.setText("🔴 开始录制")
        self.btn_record.setStyleSheet("")
        self.btn_record.setEnabled(True)
        self.lbl_status.setText("错误")
        QtWidgets.QMessageBox.warning(self, "FFmpeg 错误", f"录制失败，请检查音频设备是否被占用。\n\n详情:\n{err_msg}")


if __name__ == "__main__":
    if hasattr(QtCore.Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
        QtWidgets.QApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

    app = QtWidgets.QApplication(sys.argv)
    window = ScreenRecorderApp()
    window.show()
    sys.exit(app.exec())