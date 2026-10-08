"""Microphone selection and live levels from the assistant's capture stream."""
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QComboBox, QDialog, QLabel, QProgressBar, QPushButton, QVBoxLayout

from core.audio_input import audio_status, input_names, load_audio_config, save_microphone


class MicrophoneDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Microphone")
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Choose the microphone you speak into:"))
        self.devices = QComboBox()
        layout.addWidget(self.devices)
        self.message = QLabel()
        self.message.setWordWrap(True)
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setFormat("Input level: %v")
        layout.addWidget(self.meter)
        layout.addWidget(self.message)
        help_text = QLabel("Speak and watch the input level. The meter shows sound even while muted; muted audio is not sent to the AI.\nIf it stays at zero, check the USB connection and Windows microphone permissions.\nChanges apply while the assistant is connected. Apply also retries a failed microphone.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        refresh = QPushButton("Refresh device list")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh)
        apply = QPushButton("Apply microphone / Retry")
        apply.clicked.connect(self.apply)
        layout.addWidget(apply)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        self.refresh()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(100)

    def refresh(self):
        selected = load_audio_config().get("microphone") or ""
        self.devices.clear()
        self.devices.addItem("Windows default (automatic fallback)", "")
        try:
            names = input_names()
            if selected and selected not in names:
                self.devices.addItem(selected, selected)
            for name in names:
                self.devices.addItem(name, name)
            self.devices.setCurrentIndex(max(0, self.devices.findData(selected)))
        except Exception as exc:
            self.message.setText(str(exc))

    def apply(self):
        try:
            save_microphone(self.devices.currentData())
            self.message.setText("Applying microphone…")
        except OSError as exc:
            self.message.setText(f"Could not save microphone: {exc}")

    def poll(self):
        status = audio_status()
        self.meter.setValue(status["level"])
        self.message.setText(status["message"])
