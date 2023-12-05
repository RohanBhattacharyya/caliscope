import os
import sys
import subprocess
from pathlib import Path
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget, QVBoxLayout, QPushButton, QSpinBox, QGridLayout, QTextBrowser
from PySide6.QtCore import QFileSystemWatcher, Slot
from pyxy3d.controller import Controller
import pyxy3d.logger
logger = pyxy3d.logger.get(__name__)

class WorkspaceSummaryWidget(QWidget):
    def __init__(self, controller:Controller):
        super().__init__()

        self.controller = controller
        self.watcher = QFileSystemWatcher()

        # self.directory = QLabel(str(self.controller.workspace))
        self.open_workspace_folder_btn = QPushButton("Open Workspace Folder", self)

        self.calibrate_btn = QPushButton("Calibrate Capture Volume", self)

        self.camera_count_spin = QSpinBox()
        self.camera_count_spin.setValue(self.controller.get_camera_count())
        self.camera_count_spin.setMaximumWidth(40)

        self.status_HTML = QTextBrowser()
        # Set the layout for the widget
        self.place_widgets()
        self.connect_widgets()

        self.update_status()
        
        
    def place_widgets(self):
        # Layout
        self.layout = QGridLayout()
        self.setLayout(self.layout)
        self.layout.addWidget(self.status_HTML,0,0,1,3)

        camera_spin_layout = QHBoxLayout()
        camera_spin_layout.addWidget(QLabel("Cameras:"))
        camera_spin_layout.addWidget(self.camera_count_spin)
        self.layout.addLayout(camera_spin_layout,1,0,)
        self.layout.addWidget(self.open_workspace_folder_btn, 1,1)
        self.layout.addWidget(self.calibrate_btn,1,2)
        
    def connect_widgets(self):
        self.open_workspace_folder_btn.clicked.connect(self.open_workspace)  
        self.calibrate_btn.clicked.connect(self.on_calibrate_btn_clicked)
        self.camera_count_spin.valueChanged.connect(self.set_camera_count)
        
    def on_calibrate_btn_clicked(self):
        logger.info("Calling controller to process extrinsic streams into 2D data")
        self.controller.process_extrinsic_streams(fps_target=100)
        # Call the extrinsic calibration method in the controller
        self.controller.estimate_extrinsics()

    def set_camera_count(self, value):
        self.controller.set_camera_count(value)
        
    def open_workspace(self):
        logger.info(f"Opening workspace within File Explorer...  located at {self.controller.workspace}")
        if sys.platform == 'win32':
            os.startfile(self.controller.workspace)
        elif sys.platform == 'darwin':
            subprocess.run(["open", self.controller.workspace])
        else:  # Linux and Unix-like systems
            subprocess.run(["xdg-open", self.controller.workspace])

            
    def update_status(self):
        self.status_HTML.setHtml(self.controller.workspace_guide.get_html_summary())