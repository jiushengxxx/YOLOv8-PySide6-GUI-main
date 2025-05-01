from ultralytics import YOLO
from ultralytics.engine.results import Results
from ultralytics.utils import DEFAULT_CFG, LOGGER, SETTINGS, callbacks, ops
from ultralytics.utils.plotting import Annotator, colors, save_one_box
from ultralytics.utils.torch_utils import smart_inference_mode
from ultralytics.utils.files import increment_path
from ultralytics.utils.checks import check_imshow
from ultralytics.cfg import get_cfg
from PySide6.QtWidgets import QApplication, QMainWindow, QFileDialog, QMenu
from PySide6.QtGui import QImage, QPixmap, QColor, QAction
from PySide6.QtCore import QTimer, QThread, Signal, QObject, QPoint, Qt
from ui.CustomMessageBox import MessageBox
from ui.home import Ui_MainWindow
from UIFunctions import *
from collections import defaultdict
from pathlib import Path
from utils.capnums import Camera
from utils.rtsp_win import Window
import numpy as np
import time
import json
import torch
import sys
import cv2
import os
from threading import Thread


class YoloPredictor(QObject):
    yolo2main_pre_img = Signal(np.ndarray)   # raw image signal
    yolo2main_res_img = Signal(np.ndarray)   # test result signal
    yolo2main_status_msg = Signal(str)       # Detecting/pausing/stopping/testing complete/error reporting signal
    yolo2main_fps = Signal(str)              # fps
    yolo2main_labels = Signal(dict)          # Detected target results (number of each category)
    yolo2main_progress = Signal(int)         # Completeness
    yolo2main_class_num = Signal(int)        # Number of categories detected
    yolo2main_target_num = Signal(int)       # Targets detected

    def __init__(self, cfg=DEFAULT_CFG, overrides=None):
        super(YoloPredictor, self).__init__()
        self.yolo_model = None  # 替换原来的 self.model
        self.args = get_cfg(cfg, overrides)
        project = self.args.project or Path(SETTINGS['runs_dir']) / self.args.task
        name = f'{self.args.mode}'
        self.save_dir = increment_path(Path(project) / name, exist_ok=self.args.exist_ok)
        self.done_warmup = False
        if self.args.show:
            self.args.show = check_imshow(warn=True)

        # GUI args
        self.used_model_name = None      # The detection model name to use
        self.new_model_name = None       # Models that change in real time
        self.source = ''                 # input source
        self.stop_dtc = False            # Termination detection
        self.continue_dtc = True         # pause   
        self.save_res = False            # Save test results
        self.save_txt = False            # save label(txt) file
        self.iou_thres = 0.45            # iou
        self.conf_thres = 0.25           # conf
        self.speed_thres = 10            # delay, ms
        self.labels_dict = {}            # return a dictionary of results
        self.progress_value = 0          # progress bar
    

        # Usable if setup is done
        self.data = self.args.data  # data_dict
        self.imgsz = None
        self.device = None
        self.dataset = None
        self.vid_path, self.vid_writer = None, None
        self.annotator = None
        self.data_path = None
        self.source_type = None
        self.batch = None
        self.callbacks = defaultdict(list, callbacks.default_callbacks)  # add callbacks
        callbacks.add_integration_callbacks(self)

    # main for detect
    @smart_inference_mode()
    def run(self):
        try:
            if self.args.verbose:
                LOGGER.info('')

            # set model    
            self.yolo2main_status_msg.emit('Loding Model...')
            if not self.yolo_model:
                self.setup_model(self.new_model_name)
                self.used_model_name = self.new_model_name

            # set source
            self.setup_source(self.source if self.source is not None else self.args.source)

            # Check save path/label
            if self.save_res or self.save_txt:
                (self.save_dir / 'labels' if self.save_txt else self.save_dir).mkdir(parents=True, exist_ok=True)

            # warmup model
            if not self.done_warmup:
                self.yolo_model.warmup(imgsz=(1 if self.yolo_model.pt or self.yolo_model.triton else self.dataset.bs, 3, *self.imgsz))
                self.done_warmup = True

            self.seen, self.windows, self.dt, self.batch = 0, [], (ops.Profile(), ops.Profile(), ops.Profile()), None

            # start detection
            # for batch in self.dataset:


            count = 0                       # run location frame
            start_time = time.time()        # used to calculate the frame rate
            batch = iter(self.dataset)
            while True:
                # Termination detection
                if self.stop_dtc:
                    if isinstance(self.vid_writer[-1], cv2.VideoWriter):
                        self.vid_writer[-1].release()  # release final video writer
                    self.yolo2main_status_msg.emit('Detection terminated!')
                    break
                
                # Change the model midway
                if self.used_model_name != self.new_model_name:  
                    # self.yolo2main_status_msg.emit('Change Model...')
                    self.setup_model(self.new_model_name)
                    self.used_model_name = self.new_model_name
                
                # pause switch
                if self.continue_dtc:
                    # time.sleep(0.001)
                    self.yolo2main_status_msg.emit('Detecting...')
                    batch = next(self.dataset)  # next data

                    self.batch = batch
                    path, im, im0s, vid_cap, s = batch
                    visualize = increment_path(self.save_dir / Path(path).stem, mkdir=True) if self.args.visualize else False

                    # Calculation completion and frame rate (to be optimized)
                    count += 1              # frame count +1
                    if vid_cap:
                        all_count = vid_cap.get(cv2.CAP_PROP_FRAME_COUNT)   # total frames
                    else:
                        all_count = 1
                    self.progress_value = int(count/all_count*1000)         # progress bar(0~1000)
                    if count % 5 == 0 and count >= 5:                     # Calculate the frame rate every 5 frames
                        self.yolo2main_fps.emit(str(int(5/(time.time()-start_time))))
                        start_time = time.time()
                    
                    # preprocess 
                    with self.dt[0]:
                        im = self.preprocess(im)
                        if len(im.shape) == 3:
                            im = im[None]  # expand for batch dim
                    # inference 
                    with self.dt[1]:
                        preds = self.yolo_model(im, augment=self.args.augment, visualize=visualize)
                    # postprocess 
                    with self.dt[2]:
                        self.results = self.postprocess(preds, im, im0s)

                    # visualize, save, write results  
                    n = len(im)     # To be improved: support multiple img
                    for i in range(n):
                        self.results[i].speed = {
                            'preprocess': self.dt[0].dt * 1E3 / n,
                            'inference': self.dt[1].dt * 1E3 / n,
                            'postprocess': self.dt[2].dt * 1E3 / n}
                        p, im0 = (path[i], im0s[i].copy()) if self.source_type.webcam or self.source_type.from_img \
                            else (path, im0s.copy())
                        p = Path(p)     # the source dir

                        # s:::   video 1/1 (6/6557) 'path':
                        # must, to get boxs\labels
                        label_str = self.write_results(i, self.results, (p, im, im0))   # labels   /// original :s += 
                        
                        # labels and nums dict
                        class_nums = 0
                        target_nums = 0
                        self.labels_dict = {}
                        if 'no detections' in label_str:
                            pass
                        else:
                            for ii in label_str.split(',')[:-1]:
                                nums, label_name = ii.split('~')
                                self.labels_dict[label_name] = int(nums)
                                target_nums += int(nums)
                                class_nums += 1

                        # save img or video result
                        if self.save_res:
                            self.save_preds(vid_cap, i, str(self.save_dir / p.name))

                        # Send test results
                        self.yolo2main_res_img.emit(im0) # after detection
                        self.yolo2main_pre_img.emit(im0s if isinstance(im0s, np.ndarray) else im0s[0])   # Before testing
                        # self.yolo2main_labels.emit(self.labels_dict)        # webcam need to change the def write_results
                        self.yolo2main_class_num.emit(class_nums)
                        self.yolo2main_target_num.emit(target_nums)

                        if self.speed_thres != 0:
                            time.sleep(self.speed_thres/1000)   # delay , ms

                    self.yolo2main_progress.emit(self.progress_value)   # progress bar

                # Detection completed
                if count + 1 >= all_count:
                    if isinstance(self.vid_writer[-1], cv2.VideoWriter):
                        self.vid_writer[-1].release()  # release final video writer
                    self.yolo2main_status_msg.emit('Detection completed')
                    break

        except Exception as e:
            pass
            print(e)
            self.yolo2main_status_msg.emit('%s' % e)


    def get_annotator(self, img):
        return Annotator(img, line_width=self.args.line_thickness, example=str(self.yolo_model.names))

    def preprocess(self, img):
        img = torch.from_numpy(img).to(self.yolo_model.device)
        img = img.half() if self.yolo_model.fp16 else img.float()  # uint8 to fp16/32
        img /= 255  # 0 - 255 to 0.0 - 1.0
        return img

    def postprocess(self, preds, img, orig_img):
        ### important
        preds = ops.non_max_suppression(preds,
                                        self.conf_thres,
                                        self.iou_thres,
                                        agnostic=self.args.agnostic_nms,
                                        max_det=self.args.max_det,
                                        classes=self.args.classes)

        results = []
        for i, pred in enumerate(preds):
            orig_img = orig_img[i] if isinstance(orig_img, list) else orig_img
            shape = orig_img.shape
            pred[:, :4] = ops.scale_boxes(img.shape[2:], pred[:, :4], shape).round()
            path, _, _, _, _ = self.batch
            img_path = path[i] if isinstance(path, list) else path
            results.append(Results(orig_img=orig_img, path=img_path, names=self.yolo_model.names, boxes=pred))
        # print(results)
        return results

    def write_results(self, idx, results, batch):
        p, im, im0 = batch
        log_string = ''
        if len(im.shape) == 3:
            im = im[None]  # expand for batch dim
        self.seen += 1
        imc = im0.copy() if self.args.save_crop else im0
        if self.source_type.webcam or self.source_type.from_img:  # batch_size >= 1         # attention
            log_string += f'{idx}: '
            frame = self.dataset.count
        else:
            frame = getattr(self.dataset, 'frame', 0)
        self.data_path = p
        self.txt_path = str(self.save_dir / 'labels' / p.stem) + ('' if self.dataset.mode == 'image' else f'_{frame}')
        # log_string += '%gx%g ' % im.shape[2:]         # !!! don't add img size~
        self.annotator = self.get_annotator(im0)

        det = results[idx].boxes  # TODO: make boxes inherit from tensors

        if len(det) == 0:
            return f'{log_string}(no detections), ' # if no, send this~~

        for c in det.cls.unique():
            n = (det.cls == c).sum()  # detections per class
            log_string += f"{n}~{self.yolo_model.names[int(c)]},"   #   {'s' * (n > 1)}, "   # don't add 's'
        # now log_string is the classes 👆


        # write
        for d in reversed(det):
            cls, conf = d.cls.squeeze(), d.conf.squeeze()
            if self.save_txt:  # Write to file
                line = (cls, *(d.xywhn.view(-1).tolist()), conf) \
                    if self.args.save_conf else (cls, *(d.xywhn.view(-1).tolist()))  # label format
                with open(f'{self.txt_path}.txt', 'a') as f:
                    f.write(('%g ' * len(line)).rstrip() % line + '\n')
            if self.save_res or self.args.save_crop or self.args.show or True:  # Add bbox to image(must)
                c = int(cls)  # integer class
                name = f'id:{int(d.id.item())} {self.yolo_model.names[c]}' if d.id is not None else self.yolo_model.names[c]
                label = None if self.args.hide_labels else (name if self.args.hide_conf else f'{name} {conf:.2f}')
                self.annotator.box_label(d.xyxy.squeeze(), label, color=colors(c, True))
            if self.args.save_crop:
                save_one_box(d.xyxy,
                             imc,
                             file=self.save_dir / 'crops' / self.yolo_model.model.names[c] / f'{self.data_path.stem}.jpg',
                             BGR=True)

        return log_string
        


class MainWindow(QMainWindow, Ui_MainWindow):
    main2yolo_begin_sgl = Signal()  # The main window sends an execution signal to the yolo instance
    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)
        # basic interface
        self.setupUi(self)
        self.setAttribute(Qt.WA_TranslucentBackground)  # rounded transparent
        self.setWindowFlags(Qt.FramelessWindowHint)  # Set window flag: hide window borders
        UIFuncitons.uiDefinitions(self)
        # Show module shadows
        UIFuncitons.shadow_style(self, self.Class_QF, QColor(162,129,247))
        UIFuncitons.shadow_style(self, self.Target_QF, QColor(251, 157, 139))
        UIFuncitons.shadow_style(self, self.Fps_QF, QColor(170, 128, 213))
        UIFuncitons.shadow_style(self, self.Model_QF, QColor(64, 186, 193))
        


        # read model folder
        self.pt_list = os.listdir('./models')
        self.pt_list = [file for file in self.pt_list if file.endswith('.pt')]
        self.pt_list.sort(key=lambda x: os.path.getsize('./models/' + x))   # sort by file size
        self.model_box.clear()
        self.model_box.addItems(self.pt_list)
        self.Qtimer_ModelBox = QTimer(self)     # Timer: Monitor model file changes every 2 seconds
        self.Qtimer_ModelBox.timeout.connect(self.ModelBoxRefre)
        self.Qtimer_ModelBox.start(2000)

        # Yolo-v8 thread
        self.yolo_predict = YoloPredictor()                           # Create a Yolo instance
        self.select_model = self.model_box.currentText()                   # default model
        self.yolo_predict.new_model_name = "./models/%s" % self.select_model  
        self.yolo_thread = QThread()                                  # Create yolo thread
        self.yolo_predict.yolo2main_pre_img.connect(lambda x: self.show_image(x, self.pre_video)) 
        self.yolo_predict.yolo2main_res_img.connect(lambda x: self.show_image(x, self.res_video))
        self.yolo_predict.yolo2main_status_msg.connect(lambda x: self.show_status(x))             
        self.yolo_predict.yolo2main_fps.connect(lambda x: self.fps_label.setText(x))              
        # self.yolo_predict.yolo2main_labels.connect(self.show_labels)                            
        self.yolo_predict.yolo2main_class_num.connect(lambda x:self.Class_num.setText(str(x)))         
        self.yolo_predict.yolo2main_target_num.connect(lambda x:self.Target_num.setText(str(x)))       
        self.yolo_predict.yolo2main_progress.connect(lambda x: self.progress_bar.setValue(x))     
        self.main2yolo_begin_sgl.connect(self.yolo_predict.run)     
        self.yolo_predict.moveToThread(self.yolo_thread)              

        # Model parameters
        self.model_box.currentTextChanged.connect(self.change_model)     
        self.iou_spinbox.valueChanged.connect(lambda x:self.change_val(x, 'iou_spinbox'))    # iou box
        self.iou_slider.valueChanged.connect(lambda x:self.change_val(x, 'iou_slider'))      # iou scroll bar
        self.conf_spinbox.valueChanged.connect(lambda x:self.change_val(x, 'conf_spinbox'))  # conf box
        self.conf_slider.valueChanged.connect(lambda x:self.change_val(x, 'conf_slider'))    # conf scroll bar
        self.speed_spinbox.valueChanged.connect(lambda x:self.change_val(x, 'speed_spinbox'))# speed box
        self.speed_slider.valueChanged.connect(lambda x:self.change_val(x, 'speed_slider'))  # speed scroll bar

        # Prompt window initialization
        self.Class_num.setText('--')
        self.Target_num.setText('--')
        self.fps_label.setText('--')
        self.Model_name.setText(self.select_model)
        
        # Select detection source
        self.src_file_button.clicked.connect(self.open_src_file)  # select local file
        self.src_cam_button.clicked.connect(self.toggle_camera)  # 确保摄像头按钮信号已连接
        # self.src_rtsp_button.clicked.connect(self.show_status("The function has not yet been implemented."))#chose_rtsp

        # start testing button
        self.run_button.clicked.connect(self.run_or_continue)   # pause/start
        self.stop_button.clicked.connect(self.stop)             # termination

        # Other function buttons
        self.save_res_button.toggled.connect(self.is_save_res)  # save image option
        self.save_txt_button.toggled.connect(self.is_save_txt)  # Save label option
        self.ToggleBotton.clicked.connect(lambda: UIFuncitons.toggleMenu(self, True))   # left navigation button
        self.settings_button.clicked.connect(lambda: UIFuncitons.settingBox(self, True))   # top right settings button
        
        # initialization
        self.load_config()
        self.camera_manager = Camera()  # Initialize camera_manager
        self.init_camera()

        # 定时器用于定期读取摄像头帧
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)

        self.is_camera_running = False  # 添加摄像头状态标志

    # The main window displays the original image and detection results
    @staticmethod
    def show_image(img_src, label):
        """显示图像"""
        try:
            if img_src is None:
                return
            # 将 OpenCV 图像转换为 QImage
            ih, iw, _ = img_src.shape
            qimg = QImage(img_src.data, iw, ih, iw * 3, QImage.Format_BGR888)
            # 在 QLabel 上显示图像
            pixmap = QPixmap.fromImage(qimg)
            label.setPixmap(pixmap)
            label.setScaledContents(True)
        except Exception as e:
            print(f"显示图像时出错：{str(e)}")

    # Control start/pause
    def run_or_continue(self):
        if self.yolo_predict.source == '':
            self.show_status('Please select a video source before starting detection...')
            self.run_button.setChecked(False)
        else:
            self.yolo_predict.stop_dtc = False
            if self.run_button.isChecked():
                self.run_button.setChecked(True)    # start button
                self.save_txt_button.setEnabled(False)  # It is forbidden to check and save after starting the detection
                self.save_res_button.setEnabled(False)
                self.show_status('Detecting...')           
                self.yolo_predict.continue_dtc = True   # Control whether Yolo is paused
                if not self.yolo_thread.isRunning():
                    self.yolo_thread.start()
                    self.main2yolo_begin_sgl.emit()

            else:
                self.yolo_predict.continue_dtc = False
                self.show_status("Pause...")
                self.run_button.setChecked(False)    # start button

    # bottom status bar information
    def show_status(self, msg):
        self.status_bar.setText(msg)
        if msg == 'Detection completed' or msg == '检测完成':
            self.save_res_button.setEnabled(True)
            self.save_txt_button.setEnabled(True)
            self.run_button.setChecked(False)    
            self.progress_bar.setValue(0)
            if self.yolo_thread.isRunning():
                self.yolo_thread.quit()         # end process
        elif msg == 'Detection terminated!' or msg == '检测终止':
            self.save_res_button.setEnabled(True)
            self.save_txt_button.setEnabled(True)
            self.run_button.setChecked(False)    
            self.progress_bar.setValue(0)
            if self.yolo_thread.isRunning():
                self.yolo_thread.quit()         # end process
            self.pre_video.clear()           # clear image display  
            self.res_video.clear()          
            self.Class_num.setText('--')
            self.Target_num.setText('--')
            self.fps_label.setText('--')

    # select local file
    def open_src_file(self):
        config_file = 'config/fold.json'    
        config = json.load(open(config_file, 'r', encoding='utf-8'))
        open_fold = config['open_fold']     
        if not os.path.exists(open_fold):
            open_fold = os.getcwd()
        name, _ = QFileDialog.getOpenFileName(self, 'Video/image', open_fold, "Pic File(*.mp4 *.mkv *.avi *.flv *.jpg *.png)")
        if name:
            self.yolo_predict.source = name
            self.show_status('Load File：{}'.format(os.path.basename(name))) 
            config['open_fold'] = os.path.dirname(name)
            config_json = json.dumps(config, ensure_ascii=False, indent=2)  
            with open(config_file, 'w', encoding='utf-8') as f:
                f.write(config_json)
            self.stop()             

    # Select camera source----  have one bug
    def toggle_camera(self):
        """切换摄像头状态"""
        if not self.is_camera_running:
            self.start_camera()
        else:
            self.stop_camera()
            
    def start_camera(self):
        """启动摄像头和YOLO检测"""
        try:
            if not hasattr(self, 'yolo_thread') or not self.yolo_thread.isRunning():
                # 初始化摄像头
                self.init_camera()
                
                # 启动YOLO检测线程
                self.yolo_thread = YoloDetectionThread(model_path='yolov8n.pt')
                self.yolo_thread.raw_frame_signal.connect(self.show_raw_frame)
                self.yolo_thread.detected_frame_signal.connect(self.show_detected_frame)
                self.yolo_thread.start()
                
                # 启动帧更新定时器
                self.timer.start(30)
                
                self.is_camera_running = True
                self.show_status('摄像头已启动')
        except Exception as e:
            self.show_status(f'启动摄像头失败: {str(e)}')
            
    def stop_camera(self):
        """停止摄像头和YOLO检测"""
        if hasattr(self, 'yolo_thread') and self.yolo_thread.isRunning():
            # 停止YOLO线程
            self.yolo_thread.stop()
            self.yolo_thread.wait()
            
            # 停止定时器
            self.timer.stop()
            
            # 释放摄像头资源
            if hasattr(self.camera_manager, 'current_camera') and self.camera_manager.current_camera.isOpened():
                self.camera_manager.current_camera.release()
            
            self.is_camera_running = False
            self.show_status('摄像头已停止')

    def show_raw_frame(self, qimage):
        """显示原始帧"""
        pixmap = QPixmap.fromImage(qimage)
        self.pre_video.setPixmap(pixmap)
        self.pre_video.setScaledContents(True)

    def show_detected_frame(self, qimage):
        """显示检测结果帧"""
        pixmap = QPixmap.fromImage(qimage)
        self.res_video.setPixmap(pixmap)
        self.res_video.setScaledContents(True)

    # select network source
    def chose_rtsp(self):
        self.rtsp_window = Window()
        config_file = 'config/ip.json'
        if not os.path.exists(config_file):
            ip = "rtsp://admin:admin888@192.168.1.2:555"
            new_config = {"ip": ip}
            new_json = json.dumps(new_config, ensure_ascii=False, indent=2)
            with open(config_file, 'w', encoding='utf-8') as f:
                f.write(new_json)
        else:
            config = json.load(open(config_file, 'r', encoding='utf-8'))
            ip = config['ip']
        self.rtsp_window.rtspEdit.setText(ip)
        self.rtsp_window.show()
        self.rtsp_window.rtspButton.clicked.connect(lambda: self.load_rtsp(self.rtsp_window.rtspEdit.text()))
    
    # load network sources
    def load_rtsp(self, ip):
        try:
            self.stop()
            MessageBox(
                self.close_button, title='提示', text='加载 rtsp...', time=1000, auto=True).exec()
            self.yolo_predict.source = ip
            new_config = {"ip": ip}
            new_json = json.dumps(new_config, ensure_ascii=False, indent=2)
            with open('config/ip.json', 'w', encoding='utf-8') as f:
                f.write(new_json)
            self.show_status('Loading rtsp：{}'.format(ip))
            self.rtsp_window.close()
        except Exception as e:
            self.show_status('%s' % e)

    # Save test result button--picture/video
    def is_save_res(self):
        if self.save_res_button.checkState() == Qt.CheckState.Unchecked:
            self.show_status('NOTE: Run image results are not saved.')
            self.yolo_predict.save_res = False
        elif self.save_res_button.checkState() == Qt.CheckState.Checked:
            self.show_status('NOTE: Run image results will be saved.')
            self.yolo_predict.save_res = True
    
    # Save test result button -- label (txt)
    def is_save_txt(self):
        if self.save_txt_button.checkState() == Qt.CheckState.Unchecked:
            self.show_status('NOTE: Labels results are not saved.')
            self.yolo_predict.save_txt = False
        elif self.save_txt_button.checkState() == Qt.CheckState.Checked:
            self.show_status('NOTE: Labels results will be saved.')
            self.yolo_predict.save_txt = True

    # Configuration initialization  ~~~wait to change~~~
    def load_config(self):
        config_file = 'config/setting.json'
        if not os.path.exists(config_file):
            iou = 0.26
            conf = 0.33   
            rate = 10
            save_res = 0   
            save_txt = 0    
            new_config = {"iou": iou,
                          "conf": conf,
                          "rate": rate,
                          "save_res": save_res,
                          "save_txt": save_txt
                          }
            new_json = json.dumps(new_config, ensure_ascii=False, indent=2)
            with open(config_file, 'w', encoding='utf-8') as f:
                f.write(new_json)
        else:
            config = json.load(open(config_file, 'r', encoding='utf-8'))
            if len(config) != 5:
                iou = 0.26
                conf = 0.33
                rate = 10
                save_res = 0
                save_txt = 0
            else:
                iou = config['iou']
                conf = config['conf']
                rate = config['rate']
                save_res = config['save_res']
                save_txt = config['save_txt']
        self.save_res_button.setCheckState(Qt.CheckState(save_res))
        self.yolo_predict.save_res = (False if save_res==0 else True )
        self.save_txt_button.setCheckState(Qt.CheckState(save_txt)) 
        self.yolo_predict.save_txt = (False if save_txt==0 else True )
        self.run_button.setChecked(False)  
        self.show_status("Welcome~")

    # Terminate button and associated state
    def stop(self):
        if self.yolo_thread.isRunning():
            self.yolo_thread.quit()         # end thread
        self.yolo_predict.stop_dtc = True
        self.run_button.setChecked(False)    # start key recovery
        self.save_res_button.setEnabled(True)   # Ability to use the save button
        self.save_txt_button.setEnabled(True)   # Ability to use the save button
        self.pre_video.clear()           # clear image display
        self.res_video.clear()           # clear image display
        self.progress_bar.setValue(0)
        self.Class_num.setText('--')
        self.Target_num.setText('--')
        self.fps_label.setText('--')

    # Change detection parameters
    def change_val(self, x, flag):
        if flag == 'iou_spinbox':
            self.iou_slider.setValue(int(x * 100))
        elif flag == 'iou_slider':
            self.iou_spinbox.setValue(x / 100)
            self.show_status('IOU Threshold: %s' % str(x / 100))
            self.yolo_predict.iou_thres = x / 100
        elif flag == 'conf_spinbox':
            self.conf_slider.setValue(int(x * 100))
        elif flag == 'conf_slider':
            self.conf_spinbox.setValue(x / 100)
            self.show_status('Conf Threshold: %s' % str(x / 100))
            self.yolo_predict.conf_thres = x / 100
        elif flag == 'speed_spinbox':
            self.speed_slider.setValue(x)
            self.yolo_predict.speed_thres = x
            # 根据帧率调整延迟
            if x > 0:
                delay = 1000 / x
                self.yolo_predict.speed_thres = int(delay)
        elif flag == 'speed_slider':
            self.speed_spinbox.setValue(x)
            self.show_status('Delay: %s ms' % str(x))
            self.yolo_predict.speed_thres = x
            # 根据帧率调整延迟
            if x > 0:
                delay = 1000 / x
                self.yolo_predict.speed_thres = int(delay)
        # 动态调整帧率
        if self.yolo_predict.speed_thres > 0:
            frame_rate = 1000 / self.yolo_predict.speed_thres
            if frame_rate < 15:  # 假设帧率低于15时，适当降低阈值
                self.yolo_predict.speed_thres = max(1, self.yolo_predict.speed_thres - 1)
            elif frame_rate > 30:  # 假设帧率高于30时，适当提高阈值
                self.yolo_predict.speed_thres = self.yolo_predict.speed_thres + 1

    # change model
    def change_model(self,x):
        self.select_model = self.model_box.currentText()
        self.yolo_predict.new_model_name = "./models/%s" % self.select_model
        self.show_status('Change Model：%s' % self.select_model)
        self.Model_name.setText(self.select_model)

    # label result
    # def show_labels(self, labels_dic):
    #     try:
    #         self.result_label.clear()
    #         labels_dic = sorted(labels_dic.items(), key=lambda x: x[1], reverse=True)
    #         labels_dic = [i for i in labels_dic if i[1]>0]
    #         result = [' '+str(i[0]) + '：' + str(i[1]) for i in labels_dic]
    #         self.result_label.addItems(result)
    #     except Exception as e:
    #         self.show_status(e)

    # Cycle monitoring model file changes
    def ModelBoxRefre(self):
        pt_list = os.listdir('./models')
        pt_list = [file for file in pt_list if file.endswith('.pt')]
        pt_list.sort(key=lambda x: os.path.getsize('./models/' + x))
        # It must be sorted before comparing, otherwise the list will be refreshed all the time
        if pt_list != self.pt_list:
            self.pt_list = pt_list
            self.model_box.clear()
            self.model_box.addItems(self.pt_list)

    # Get the mouse position (used to hold down the title bar and drag the window)
    def mousePressEvent(self, event):
        p = event.globalPosition()
        globalPos = p.toPoint()
        self.dragPos = globalPos

    # Optimize the adjustment when dragging the bottom and right edges of the window size
    def resizeEvent(self, event):
        # Update Size Grips
        UIFuncitons.resize_grips(self)

    # Exit Exit thread, save settings
    def closeEvent(self, event):
        """重写窗口关闭事件"""
        # 停止 YOLO 检测线程
        if self.yolo_thread.isRunning():
            self.yolo_predict.stop_dtc = True
            self.yolo_thread.quit()
            self.yolo_thread.wait()

        # 释放摄像头资源
        if hasattr(self, 'cap') and self.cap.isOpened():
            self.cap.release()

        # 保存配置
        self.save_settings()

        # 接受关闭事件
        event.accept()

    def init_camera(self):
        """初始化摄像头"""
        try:
            _, available_cameras = self.camera_manager.get_cam_num()
            if available_cameras:
                self.camera_manager.add_camera(available_cameras[0])
                self.camera_manager.switch_camera(available_cameras[0])
                self.show_status(f'摄像头 {available_cameras[0]} 连接成功')
            else:
                self.show_status('未检测到可用摄像头')
        except Exception as e:
            self.show_status(f'摄像头连接失败: {str(e)}')

    def update_frame(self):
        """更新摄像头帧到 GUI"""
        try:
            ret, frame = self.camera_manager.current_camera.read()
            if ret:
                # 将帧转换为 QImage
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = frame.shape
                bytes_per_line = ch * w
                q_img = QImage(frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
                # 更新到 QLabel
                self.pre_video.setPixmap(QPixmap.fromImage(q_img))
        except Exception as e:
            self.show_status(f'帧更新出错: {str(e)}')


class YoloDetectionThread(QThread):
    raw_frame_signal = Signal(QImage)
    detected_frame_signal = Signal(QImage)
    
    def __init__(self, model_path, camera_index=0):
        super().__init__()
        self.model_path = model_path
        self.camera_index = camera_index
        self.is_running = True
        
    def run(self):
        try:
            model = YOLO(self.model_path)
            cap = cv2.VideoCapture(self.camera_index)
            
            while self.is_running:
                ret, frame = cap.read()
                if ret:
                    # 发送原始帧a
                    rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    h, w, ch = rgb_image.shape
                    bytes_per_line = ch * w
                    q_img = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format_RGB888)
                    self.raw_frame_signal.emit(q_img)
                    
                    # 进行目标检测
                    results = model(frame)
                    annotated_frame = results[0].plot()
                    
                    # 发送检测结果帧
                    rgb_detected = cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB)
                    q_detected = QImage(rgb_detected.data, w, h, bytes_per_line, QImage.Format_RGB888)
                    self.detected_frame_signal.emit(q_detected)
                    
            cap.release()
        except Exception as e:
            print(f"检测出错: {str(e)}")
            
    def stop(self):
        self.is_running = False
        self.wait()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    Home = MainWindow()
    Home.show()
    sys.exit(app.exec())