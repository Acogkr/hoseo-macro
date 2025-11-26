import sys
import os
import json
import time
import threading
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QLabel, QLineEdit, QPushButton, QCheckBox, QStackedWidget, 
                               QTableWidget, QTableWidgetItem, QHeaderView, QProgressBar, 
                               QTextEdit, QMessageBox, QFrame, QSpacerItem, QSizePolicy)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QSize
from PySide6.QtGui import QFont, QIcon, QColor, QPalette

import hoseo_crawler
import config_manager

if sys.platform == 'win32':
    import ctypes
    myappid = 'hoseo.lms.automation.pyside6.1.0'
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)

DARK_THEME_QSS = """
QMainWindow {
    background-color: #1e1e1e;
}
QWidget {
    background-color: #1e1e1e;
    color: #ffffff;
    font-family: 'Malgun Gothic', sans-serif;
    font-size: 14px;
}
QFrame#Card {
    background-color: #2d2d2d;
    border-radius: 15px;
    border: none;
}
QLineEdit {
    background-color: #3d3d3d;
    border: 1px solid #555555;
    border-radius: 5px;
    padding: 10px;
    color: #ffffff;
    font-size: 14px;
}
QLineEdit:focus {
    border: 1px solid #1F6AA5;
}
QPushButton {
    background-color: #1F6AA5;
    color: white;
    border: none;
    border-radius: 5px;
    padding: 10px 20px;
    font-weight: bold;
    font-size: 14px;
}
QPushButton:hover {
    background-color: #144870;
}
QPushButton:disabled {
    background-color: #555555;
    color: #aaaaaa;
}
QPushButton#StopButton {
    background-color: #C0392B;
}
QPushButton#StopButton:hover {
    background-color: #962D22;
}
QPushButton#ResetButton {
    background-color: #7F8C8D;
}
QPushButton#ResetButton:hover {
    background-color: #626D6E;
}
QTableWidget {
    background-color: #2d2d2d;
    gridline-color: #3d3d3d;
    border: 1px solid #3d3d3d;
    border-radius: 5px;
}
QTableWidget::item {
    padding: 5px;
}
QHeaderView::section {
    background-color: #3d3d3d;
    padding: 5px;
    border: none;
    font-weight: bold;
}
QProgressBar {
    border: 1px solid #3d3d3d;
    border-radius: 5px;
    text-align: center;
    background-color: #2d2d2d;
}
QProgressBar::chunk {
    background-color: #1F6AA5;
    border-radius: 5px;
}
QTextEdit {
    background-color: #121212;
    border: 1px solid #3d3d3d;
    border-radius: 5px;
    font-family: 'Consolas', monospace;
    font-size: 12px;
    color: #cccccc;
}
QCheckBox {
    spacing: 5px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
}
QLabel {
    background-color: transparent;
}
QCheckBox {
    background-color: transparent;
}
QFrame#Card QWidget {
    background-color: transparent;
}
"""

class LoginWorker(QThread):
    finished = Signal(bool, list, str)

    def __init__(self, user_id, password):
        super().__init__()
        self.user_id = user_id
        self.password = password

    def run(self):
        try:
            driver, wait = hoseo_crawler.init_driver()
            if hoseo_crawler.login(driver, wait, self.user_id, self.password):
                courses = hoseo_crawler.scan_courses(driver, wait)
                self.finished.emit(True, courses, "로그인 성공")
                self.driver = driver
                self.wait = wait
            else:
                driver.quit()
                self.finished.emit(False, [], "로그인 실패: 아이디/비밀번호를 확인해주세요.")
        except Exception as e:
            self.finished.emit(False, [], f"오류 발생: {str(e)}")

class AutomationWorker(QThread):
    log_signal = Signal(str)
    progress_signal = Signal(int, int, str)
    video_progress_signal = Signal(int, int, str)
    finished_signal = Signal()

    def __init__(self, driver, wait, selected_courses, stop_event, user_id, password):
        super().__init__()
        self.driver = driver
        self.wait = wait
        self.selected_courses = selected_courses
        self.stop_event = stop_event
        self.user_id = user_id
        self.password = password

    def run(self):
        def log_callback(msg):
            self.log_signal.emit(msg)
        
        def video_progress_callback(current_time, duration, title):
            self.video_progress_signal.emit(current_time, duration, title)
        
        hoseo_crawler.set_log_callback(log_callback)
        hoseo_crawler.set_video_progress_callback(video_progress_callback)

        total = len(self.selected_courses)
        for i, course in enumerate(self.selected_courses):
            if self.stop_event.is_set():
                break
            
            self.progress_signal.emit(i, total, f"진행 중: {course['class_name']}")
            self.log_signal.emit(f"[{course['class_name']}] 강의 처리를 시작합니다.")
            
            success, new_driver, new_wait = hoseo_crawler.process_course_with_recovery(
                self.driver, self.wait, course, self.stop_event,
                self.user_id, self.password, log_callback
            )
            
            self.driver = new_driver
            self.wait = new_wait
            
            if success:
                self.progress_signal.emit(i + 1, total, f"완료: {course['class_name']}")
            else:
                self.log_signal.emit(f"[{course['class_name']}] 처리를 완료하지 못했습니다.")

        if self.stop_event.is_set():
            self.log_signal.emit("사용자 요청에 의해 작업이 중지되었습니다.")
        else:
            self.log_signal.emit("모든 작업이 완료되었습니다.")
        
        self.finished_signal.emit()

class LoginWidget(QWidget):
    login_requested = Signal(str, str, bool)

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)

        card = QFrame(self)
        card.setObjectName("Card")
        card.setFixedSize(400, 500)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(40, 40, 40, 40)
        card_layout.setSpacing(20)

        title = QLabel("호서대학교\nLMS 자동 수강")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Malgun Gothic", 24, QFont.Bold))
        card_layout.addWidget(title)

        card_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("학번 (ID)")
        card_layout.addWidget(self.id_input)

        self.pw_input = QLineEdit()
        self.pw_input.setPlaceholderText("비밀번호 (Password)")
        self.pw_input.setEchoMode(QLineEdit.Password)
        self.pw_input.returnPressed.connect(self.on_login_clicked)
        card_layout.addWidget(self.pw_input)

        self.remember_cb = QCheckBox("아이디/비밀번호 저장")
        card_layout.addWidget(self.remember_cb)

        self.login_btn = QPushButton("로그인 및 강의 스캔")
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.clicked.connect(self.on_login_clicked)
        self.login_btn.setFixedHeight(45)
        self.login_btn.setStyleSheet("background-color: #3d3d3d; border: 1px solid #555555;")
        card_layout.addWidget(self.login_btn)

        self.status_lbl = QLabel("")
        self.status_lbl.setAlignment(Qt.AlignCenter)
        self.status_lbl.setStyleSheet("color: #FF4B4B; font-size: 12px;")
        card_layout.addWidget(self.status_lbl)

        card_layout.addSpacerItem(QSpacerItem(20, 20, QSizePolicy.Minimum, QSizePolicy.Expanding))

        layout.addWidget(card)

    def on_login_clicked(self):
        user_id = self.id_input.text().strip()
        pw = self.pw_input.text().strip()
        if not user_id or not pw:
            self.status_lbl.setText("학번과 비밀번호를 입력해주세요.")
            return
        
        self.status_lbl.setText("로그인 중입니다...")
        self.status_lbl.setStyleSheet("color: #2CC985;")
        self.login_btn.setEnabled(False)
        self.login_requested.emit(user_id, pw, self.remember_cb.isChecked())

    def set_status(self, msg, is_error=True):
        self.status_lbl.setText(msg)
        self.status_lbl.setStyleSheet("color: #FF4B4B;" if is_error else "color: #2CC985;")
        self.login_btn.setEnabled(True)

class DashboardWidget(QWidget):
    logout_requested = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        header_layout = QHBoxLayout()
        title = QLabel("수강 대시보드")
        title.setFont(QFont("Malgun Gothic", 18, QFont.Bold))
        header_layout.addWidget(title)
        
        header_layout.addStretch()
        
        logout_btn = QPushButton("로그아웃")
        logout_btn.setFixedSize(100, 35)
        logout_btn.setStyleSheet("background-color: #555555;")
        logout_btn.clicked.connect(self.logout_requested.emit)
        header_layout.addWidget(logout_btn)
        
        layout.addLayout(header_layout)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["선택", "강의명", "미수강", "예상 시간"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 50)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.NoSelection)
        layout.addWidget(self.table)

        self.progress_lbl = QLabel("대기 중...")
        layout.addWidget(self.progress_lbl)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(True)
        layout.addWidget(self.progress_bar)

        btn_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("자동 수강 시작")
        self.start_btn.setFixedSize(150, 45)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        btn_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("정지")
        self.stop_btn.setObjectName("StopButton")
        self.stop_btn.setFixedSize(100, 45)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)
        
        btn_layout.addStretch()
        
        self.verbose_chk = QCheckBox("자세한 로그")
        self.verbose_chk.setCursor(Qt.PointingHandCursor)
        self.verbose_chk.stateChanged.connect(self.toggle_verbose)
        btn_layout.addWidget(self.verbose_chk)
        
        layout.addLayout(btn_layout)

        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        layout.addWidget(self.log_area)

        self.courses = []
        self.checkboxes = []

    def toggle_verbose(self, state):
        is_checked = self.verbose_chk.isChecked()
        hoseo_crawler.set_verbose(is_checked)
        if is_checked:
            self.append_log("상세 로그 모드가 켜졌습니다.")
        else:
            self.append_log("상세 로그 모드가 꺼졌습니다.")

    def get_selected_courses(self):
        selected = []
        for i, chk in enumerate(self.checkboxes):
            if chk.isChecked():
                selected.append(self.courses[i])
        return selected

    def load_courses(self, courses):
        self.courses = courses
        self.table.setRowCount(len(courses))
        self.checkboxes = []
        
        saved_selection = None
        try:
            config = config_manager.load_config()
            saved_selection = config.get("selected_courses")
        except:
            pass

        for i, course in enumerate(courses):
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.setContentsMargins(0, 0, 0, 0)
            chk_layout.setAlignment(Qt.AlignCenter)
            chk = QCheckBox()
            
            is_checked = True
            if saved_selection is not None:
                is_checked = course['class_name'] in saved_selection
            chk.setChecked(is_checked)
            
            chk_layout.addWidget(chk)
            self.table.setCellWidget(i, 0, chk_widget)
            self.checkboxes.append(chk)

            self.table.setItem(i, 1, QTableWidgetItem(course['class_name']))
            
            count = course.get('uncompleted_count', 0)
            item_count = QTableWidgetItem(f"{count}개")
            item_count.setTextAlignment(Qt.AlignCenter)
            if count > 0:
                item_count.setForeground(QColor("#FF4B4B"))
            else:
                item_count.setForeground(QColor("#2CC985"))
            self.table.setItem(i, 2, item_count)

            est_time = f"약 {count * 20}분" if count > 0 else "-"
            item_time = QTableWidgetItem(est_time)
            item_time.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(i, 3, item_time)

    def append_log(self, msg):
        import datetime
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_area.append(f"[{timestamp}] {msg}")
        sb = self.log_area.verticalScrollBar()
        sb.setValue(sb.maximum())

class HoseoLMSApp(QMainWindow):
    def resource_path(self, relative_path):
        try:
            base_path = sys._MEIPASS
        except Exception:
            base_path = os.path.abspath(".")

        return os.path.join(base_path, relative_path)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("호서대학교 매크로")
        self.resize(900, 700)
        
        icon_path = self.resource_path("hoseo_logo.png")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        
        self.driver = None
        self.wait = None
        self.stop_event = threading.Event()

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.login_page = LoginWidget()
        self.dashboard_page = DashboardWidget()
        
        self.stack.addWidget(self.login_page)
        self.stack.addWidget(self.dashboard_page)

        self.login_page.login_requested.connect(self.handle_login)
        self.dashboard_page.logout_requested.connect(self.handle_logout)
        
        self.dashboard_page.start_btn.clicked.connect(self.start_automation)
        self.dashboard_page.stop_btn.clicked.connect(self.stop_automation)

        self.load_config()

    def load_config(self):
        try:
            config = config_manager.load_config()
            if config.get("remember_me"):
                self.login_page.id_input.setText(config.get("user_id", ""))
                self.login_page.pw_input.setText(config.get("password", ""))
                self.login_page.remember_cb.setChecked(True)
        except Exception as e:
            print(f"설정 로드 오류: {e}")

    def save_config(self, user_id, password, remember, selected_courses=None):
        try:
            config_manager.save_config(user_id, password, remember, selected_courses)
        except Exception as e:
            print(f"설정 저장 오류: {e}")

    def handle_login(self, user_id, password, remember):
        self.save_config(user_id, password, remember)
        
        self.login_worker = LoginWorker(user_id, password)
        self.login_worker.finished.connect(self.on_login_finished)
        self.login_worker.start()

    def on_login_finished(self, success, courses, msg):
        if success:
            self.driver = self.login_worker.driver
            self.wait = self.login_worker.wait
            self.dashboard_page.load_courses(courses)
            self.stack.setCurrentWidget(self.dashboard_page)
        else:
            self.login_page.set_status(msg)

    def handle_logout(self):
        if self.driver:
            self.driver.quit()
            self.driver = None
        self.stack.setCurrentWidget(self.login_page)
        self.login_page.status_lbl.setText("")
        self.login_page.login_btn.setEnabled(True)

    def start_automation(self):
        selected = self.dashboard_page.get_selected_courses()
        if not selected:
            self.dashboard_page.append_log("선택된 강의가 없습니다.")
            return

        user_id = self.login_page.id_input.text()
        pw = self.login_page.pw_input.text()
        remember = self.login_page.remember_cb.isChecked()
        self.save_config(user_id, pw, remember, [c['class_name'] for c in selected])

        self.stop_event.clear()
        self.dashboard_page.start_btn.setEnabled(False)
        self.dashboard_page.stop_btn.setEnabled(True)

        self.dashboard_page.progress_bar.setValue(0)
        
        self.worker = AutomationWorker(
            self.driver, self.wait, selected, self.stop_event, user_id, pw
        )
        self.worker.log_signal.connect(self.dashboard_page.append_log)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.video_progress_signal.connect(self.update_video_progress)
        self.worker.finished_signal.connect(self.on_automation_finished)
        self.worker.start()

    def update_progress(self, current, total, status):
        self.dashboard_page.progress_lbl.setText(status)
        if total > 0:
            progress = int((current / total) * 100)
            self.dashboard_page.progress_bar.setValue(progress)
        else:
            self.dashboard_page.progress_bar.setValue(0)
    
    def update_video_progress(self, current_time, duration, title):
        if duration > 0:
            progress = int((current_time / duration) * 100)
            self.dashboard_page.progress_bar.setValue(progress)
            time_str = f"{int(current_time//60)}:{int(current_time%60):02d} / {int(duration//60)}:{int(duration%60):02d}"
            self.dashboard_page.progress_lbl.setText(f"재생 중: {title} ({time_str})")

    def stop_automation(self):
        self.dashboard_page.append_log("중지 요청을 확인했습니다. 현재 작업이 끝나면 멈춥니다.")
        self.stop_event.set()
        self.dashboard_page.stop_btn.setEnabled(False)

    def on_automation_finished(self):
        self.dashboard_page.start_btn.setEnabled(True)
        self.dashboard_page.stop_btn.setEnabled(False)
        self.dashboard_page.progress_lbl.setText("완료")
        self.dashboard_page.progress_bar.setValue(100)

    def closeEvent(self, event):
        self.stop_event.set()
        if self.driver:
            self.driver.quit()
        event.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME_QSS)
    
    window = HoseoLMSApp()
    window.show()
    
    sys.exit(app.exec())
