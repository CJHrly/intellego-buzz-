from typing import Dict, Tuple, List, Optional

from PyQt6 import QtGui
from PyQt6.QtCore import (
    Qt,
    QThread,
    QModelIndex,
)
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QFileDialog,
)

from buzz.cache import TasksCache
from buzz.file_transcriber_queue_worker import FileTranscriberQueueWorker
from buzz.locale import _
from buzz.settings.settings import APP_NAME, Settings
from buzz.settings.shortcut_settings import ShortcutSettings
from buzz.store.keyring_store import KeyringStore
from buzz.transcriber.transcriber import (
    FileTranscriptionTask,
    TranscriptionOptions,
    FileTranscriptionOptions,
    SUPPORTED_AUDIO_FORMATS,
)
from buzz.widgets.icon import BUZZ_ICON_PATH
from buzz.widgets.import_url_dialog import ImportURLDialog
from buzz.widgets.main_window_toolbar import MainWindowToolbar
from buzz.widgets.menu_bar import MenuBar
from buzz.widgets.preferences_dialog.models.preferences import Preferences
from buzz.widgets.transcriber.file_transcriber_widget import FileTranscriberWidget
from buzz.widgets.transcription_task_folder_watcher import (
    TranscriptionTaskFolderWatcher,
)
from buzz.widgets.transcription_tasks_table_widget import TranscriptionTasksTableWidget
from buzz.widgets.transcription_viewer.transcription_viewer_widget import (
    TranscriptionViewerWidget,
)


class MainWindow(QMainWindow):
    table_widget: TranscriptionTasksTableWidget
    tasks: Dict[int, "FileTranscriptionTask"]

    def __init__(self, tasks_cache=TasksCache()):
        super().__init__(flags=Qt.WindowType.Window)

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(QIcon(BUZZ_ICON_PATH))
        self.setMinimumSize(450, 400)

        self.setAcceptDrops(True)

        self.tasks_cache = tasks_cache

        self.settings = Settings()

        self.shortcut_settings = ShortcutSettings(settings=self.settings)
        self.shortcuts = self.shortcut_settings.load()
        self.default_export_file_name = self.settings.value(
            Settings.Key.DEFAULT_EXPORT_FILE_NAME,
            "{{ input_file_name }} ({{ task }}d on {{ date_time }})",
        )

        self.tasks = {}

        self.toolbar = MainWindowToolbar(shortcuts=self.shortcuts, parent=self)
        self.toolbar.new_transcription_action_triggered.connect(
            self.on_new_transcription_action_triggered
        )
        self.toolbar.open_transcript_action_triggered.connect(
            self.open_transcript_viewer
        )
        self.toolbar.clear_history_action_triggered.connect(
            self.on_clear_history_action_triggered
        )
        self.toolbar.stop_transcription_action_triggered.connect(
            self.on_stop_transcription_action_triggered
        )
        self.addToolBar(self.toolbar)
        self.setUnifiedTitleAndToolBarOnMac(True)

        self.preferences = self.load_preferences(settings=self.settings)
        self.menu_bar = MenuBar(
            shortcuts=self.shortcuts,
            default_export_file_name=self.default_export_file_name,
            preferences=self.preferences,
            parent=self,
        )
        self.menu_bar.import_action_triggered.connect(
            self.on_new_transcription_action_triggered
        )
        self.menu_bar.import_url_action_triggered.connect(
            self.on_new_url_transcription_action_triggered
        )
        self.menu_bar.shortcuts_changed.connect(self.on_shortcuts_changed)
        self.menu_bar.openai_api_key_changed.connect(
            self.on_openai_access_token_changed
        )
        self.menu_bar.default_export_file_name_changed.connect(
            self.default_export_file_name_changed
        )
        self.menu_bar.preferences_changed.connect(self.on_preferences_changed)
        self.setMenuBar(self.menu_bar)

        self.table_widget = TranscriptionTasksTableWidget(self)
        self.table_widget.doubleClicked.connect(self.on_table_double_clicked)
        self.table_widget.return_clicked.connect(self.open_transcript_viewer)
        self.table_widget.itemSelectionChanged.connect(self.on_table_selection_changed)

        self.setCentralWidget(self.table_widget)

        # Start transcriber thread
        self.transcriber_thread = QThread()

        self.transcriber_worker = FileTranscriberQueueWorker()
        self.transcriber_worker.moveToThread(self.transcriber_thread)

        self.transcriber_worker.task_updated.connect(self.update_task_table_row)
        self.transcriber_worker.completed.connect(self.transcriber_thread.quit)

        self.transcriber_thread.started.connect(self.transcriber_worker.run)

        self.transcriber_thread.start()

        self.load_tasks_from_cache()

        self.load_geometry()

        self.folder_watcher = TranscriptionTaskFolderWatcher(
            tasks=self.tasks,
            preferences=self.preferences.folder_watch,
            default_export_file_name=self.default_export_file_name,
        )
        self.folder_watcher.task_found.connect(self.add_task)
        self.folder_watcher.find_tasks()

    def on_preferences_changed(self, preferences: Preferences):
        self.preferences = preferences
        self.save_preferences(preferences)
        self.folder_watcher.set_preferences(preferences.folder_watch)
        self.folder_watcher.find_tasks()

    def save_preferences(self, preferences: Preferences):
        self.settings.settings.beginGroup("preferences")
        preferences.save(self.settings.settings)
        self.settings.settings.endGroup()

    def load_preferences(self, settings: Settings):
        settings.settings.beginGroup("preferences")
        preferences = Preferences.load(settings.settings)
        settings.settings.endGroup()
        return preferences

    def dragEnterEvent(self, event):
        # Accept file drag events
        if event.mimeData().hasUrls():
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        file_paths = [url.toLocalFile() for url in event.mimeData().urls()]
        self.open_file_transcriber_widget(file_paths=file_paths)

    def on_file_transcriber_triggered(
        self, options: Tuple[TranscriptionOptions, FileTranscriptionOptions, str]
    ):
        transcription_options, file_transcription_options, model_path = options

        if file_transcription_options.file_paths is not None:
            for file_path in file_transcription_options.file_paths:
                task = FileTranscriptionTask(
                    transcription_options=transcription_options,
                    file_transcription_options=file_transcription_options,
                    model_path=model_path,
                    file_path=file_path,
                    source=FileTranscriptionTask.Source.FILE_IMPORT,
                )
                self.add_task(task)
        else:
            task = FileTranscriptionTask(
                transcription_options=transcription_options,
                file_transcription_options=file_transcription_options,
                model_path=model_path,
                url=file_transcription_options.url,
                source=FileTranscriptionTask.Source.URL_IMPORT,
            )
            self.add_task(task)

    def upsert_task_in_table(self, task: FileTranscriptionTask):
        self.table_widget.upsert_task(task)
        self.tasks[task.id] = task

    def update_task_table_row(self, task: FileTranscriptionTask):
        self.upsert_task_in_table(task=task)
        self.on_tasks_changed()

    @staticmethod
    def task_completed_or_errored(task: FileTranscriptionTask):
        return (
            task.status == FileTranscriptionTask.Status.COMPLETED
            or task.status == FileTranscriptionTask.Status.FAILED
        )

    def on_clear_history_action_triggered(self):
        selected_rows = self.table_widget.selectionModel().selectedRows()
        if len(selected_rows) == 0:
            return

        reply = QMessageBox.question(
            self,
            _("Clear History"),
            _(
                "Are you sure you want to delete the selected transcription(s)? "
                "This action cannot be undone."
            ),
        )
        if reply == QMessageBox.StandardButton.Yes:
            task_ids = [
                TranscriptionTasksTableWidget.find_task_id(selected_row)
                for selected_row in selected_rows
            ]
            for task_id in task_ids:
                self.table_widget.clear_task(task_id)
                self.tasks.pop(task_id)
                self.on_tasks_changed()

    def on_stop_transcription_action_triggered(self):
        selected_rows = self.table_widget.selectionModel().selectedRows()
        for selected_row in selected_rows:
            task_id = TranscriptionTasksTableWidget.find_task_id(selected_row)
            task = self.tasks[task_id]

            task.status = FileTranscriptionTask.Status.CANCELED
            self.on_tasks_changed()
            self.transcriber_worker.cancel_task(task_id)
            self.table_widget.upsert_task(task)

    def on_new_transcription_action_triggered(self):
        (file_paths, __) = QFileDialog.getOpenFileNames(
            self, _("Select audio file"), "", SUPPORTED_AUDIO_FORMATS
        )
        if len(file_paths) == 0:
            return

        self.open_file_transcriber_widget(file_paths)

    def on_new_url_transcription_action_triggered(self):
        url = ImportURLDialog.prompt(parent=self)
        if url is not None:
            self.open_file_transcriber_widget(url=url)

    def open_file_transcriber_widget(
        self, file_paths: Optional[List[str]] = None, url: Optional[str] = None
    ):
        file_transcriber_window = FileTranscriberWidget(
            file_paths=file_paths,
            url=url,
            default_output_file_name=self.default_export_file_name,
            parent=self,
            flags=Qt.WindowType.Window,
        )
        file_transcriber_window.triggered.connect(self.on_file_transcriber_triggered)
        file_transcriber_window.openai_access_token_changed.connect(
            self.on_openai_access_token_changed
        )
        file_transcriber_window.show()

    @staticmethod
    def on_openai_access_token_changed(access_token: str):
        KeyringStore().set_password(KeyringStore.Key.OPENAI_API_KEY, access_token)

    def default_export_file_name_changed(self, default_export_file_name: str):
        self.default_export_file_name = default_export_file_name
        self.settings.set_value(
            Settings.Key.DEFAULT_EXPORT_FILE_NAME, default_export_file_name
        )
        self.folder_watcher.default_export_file_name = default_export_file_name

    def open_transcript_viewer(self):
        selected_rows = self.table_widget.selectionModel().selectedRows()
        for selected_row in selected_rows:
            task_id = TranscriptionTasksTableWidget.find_task_id(selected_row)
            self.open_transcription_viewer(task_id)

    def on_table_selection_changed(self):
        self.toolbar.set_open_transcript_action_enabled(
            self.should_enable_open_transcript_action()
        )
        self.toolbar.set_stop_transcription_action_enabled(
            self.should_enable_stop_transcription_action()
        )
        self.toolbar.set_clear_history_action_enabled(
            self.should_enable_clear_history_action()
        )

    def should_enable_open_transcript_action(self):
        return self.selected_tasks_have_status([FileTranscriptionTask.Status.COMPLETED])

    def should_enable_stop_transcription_action(self):
        return self.selected_tasks_have_status(
            [
                FileTranscriptionTask.Status.IN_PROGRESS,
                FileTranscriptionTask.Status.QUEUED,
            ]
        )

    def should_enable_clear_history_action(self):
        return self.selected_tasks_have_status(
            [
                FileTranscriptionTask.Status.COMPLETED,
                FileTranscriptionTask.Status.FAILED,
                FileTranscriptionTask.Status.CANCELED,
            ]
        )

    def selected_tasks_have_status(self, statuses: List[FileTranscriptionTask.Status]):
        selected_rows = self.table_widget.selectionModel().selectedRows()
        if len(selected_rows) == 0:
            return False
        return all(
            [
                self.tasks[
                    TranscriptionTasksTableWidget.find_task_id(selected_row)
                ].status
                in statuses
                for selected_row in selected_rows
            ]
        )

    def on_table_double_clicked(self, index: QModelIndex):
        task_id = TranscriptionTasksTableWidget.find_task_id(index)
        self.open_transcription_viewer(task_id)

    def open_transcription_viewer(self, task_id: int):
        task = self.tasks[task_id]
        if task.status != FileTranscriptionTask.Status.COMPLETED:
            return

        transcription_viewer_widget = TranscriptionViewerWidget(
            transcription_task=task, parent=self, flags=Qt.WindowType.Window
        )
        transcription_viewer_widget.task_changed.connect(self.on_tasks_changed)
        transcription_viewer_widget.show()

    def add_task(self, task: FileTranscriptionTask):
        self.transcriber_worker.add_task(task)

    def load_tasks_from_cache(self):
        tasks = self.tasks_cache.load()
        for task in tasks:
            if (
                task.status == FileTranscriptionTask.Status.QUEUED
                or task.status == FileTranscriptionTask.Status.IN_PROGRESS
            ):
                task.status = None
                self.add_task(task)
            else:
                self.upsert_task_in_table(task=task)

    def save_tasks_to_cache(self):
        self.tasks_cache.save(list(self.tasks.values()))

    def on_tasks_changed(self):
        self.toolbar.set_open_transcript_action_enabled(
            self.should_enable_open_transcript_action()
        )
        self.toolbar.set_stop_transcription_action_enabled(
            self.should_enable_stop_transcription_action()
        )
        self.toolbar.set_clear_history_action_enabled(
            self.should_enable_clear_history_action()
        )
        self.save_tasks_to_cache()

    def on_shortcuts_changed(self, shortcuts: dict):
        self.shortcuts = shortcuts
        self.menu_bar.set_shortcuts(shortcuts=self.shortcuts)
        self.toolbar.set_shortcuts(shortcuts=self.shortcuts)
        self.shortcut_settings.save(shortcuts=self.shortcuts)

    def resizeEvent(self, event):
        self.save_geometry()

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.save_geometry()

        self.transcriber_worker.stop()
        self.transcriber_thread.quit()
        self.transcriber_thread.wait()
        self.save_tasks_to_cache()
        self.shortcut_settings.save(shortcuts=self.shortcuts)
        super().closeEvent(event)

    def save_geometry(self):
        self.settings.begin_group(Settings.Key.MAIN_WINDOW)
        self.settings.settings.setValue("geometry", self.saveGeometry())
        self.settings.end_group()

    def load_geometry(self):
        self.settings.begin_group(Settings.Key.MAIN_WINDOW)
        geometry = self.settings.settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.settings.end_group()
