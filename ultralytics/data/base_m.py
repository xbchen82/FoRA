# Ultralytics YOLO 🚀, AGPL-3.0 license

import glob
import math
import os
import random
from copy import deepcopy
from multiprocessing.pool import ThreadPool
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
import psutil
from torch.utils.data import Dataset

from ultralytics.utils import DEFAULT_CFG, LOCAL_RANK, LOGGER, NUM_THREADS, TQDM
from .utils import HELP_URL, IMG_FORMATS


class BaseDataset_m(Dataset):
    """
    Base dataset class for loading and processing image data.

    Args:
        img_path (str): Path to the folder containing images.
        imgsz (int, optional): Image size. Defaults to 640.
        cache (bool, optional): Cache images to RAM or disk during training. Defaults to False.
        augment (bool, optional): If True, data augmentation is applied. Defaults to True.
        hyp (dict, optional): Hyperparameters to apply data augmentation. Defaults to None.
        prefix (str, optional): Prefix to print in log messages. Defaults to ''.
        rect (bool, optional): If True, rectangular training is used. Defaults to False.
        batch_size (int, optional): Size of batches. Defaults to None.
        stride (int, optional): Stride. Defaults to 32.
        pad (float, optional): Padding. Defaults to 0.0.
        single_cls (bool, optional): If True, single class training is used. Defaults to False.
        classes (list): List of included classes. Default is None.
        fraction (float): Fraction of dataset to utilize. Default is 1.0 (use all data).

    Attributes:
        im_files (list): List of image file paths.
        labels (list): List of label data dictionaries.
        ni (int): Number of images in the dataset.
        ims (list): List of loaded images.
        npy_files (list): List of numpy file paths.
        transforms (callable): Image transformation function.
    """

    def __init__(
        self,
        img_path_rgb,
        img_path_ir,
        imgsz=640,
        cache=False,
        augment=True,
        hyp=DEFAULT_CFG,
        prefix_rgb="",
        prefix_ir="",
        rect=False,
        batch_size=16,
        stride=32,
        pad=0.5,
        single_cls=False,
        classes=None,
        fraction=1.0,
    ):
        """Initialize BaseDataset with given configuration and options."""
        super().__init__()
        self.img_path_rgb = img_path_rgb
        self.img_path_ir = img_path_ir
        self.imgsz = imgsz
        self.augment = augment
        self.single_cls = single_cls
        self.prefix_rgb = prefix_rgb
        self.prefix_ir = prefix_ir
        self.fraction = fraction
        self.im_files_rgb = self.get_img_files(self.img_path_rgb, self.prefix_rgb)
        self.im_files_ir = self.get_img_files(self.img_path_ir, self.prefix_ir)
        self.labels_rgb = self.get_labels_rgb()
        self.labels_ir = self.get_labels_ir()
        self.update_labels(include_class=classes)  # single_cls and include_class
        self.ni = len(self.labels_rgb)  # number of images
        self.rect = rect
        self.batch_size = batch_size
        self.stride = stride
        self.pad = pad
        if self.rect:
            assert self.batch_size is not None
            self.set_rectangle()

        # Buffer thread for mosaic images
        self.buffer_rgb = []  # buffer size = batch size
        self.buffer_ir = []  # buffer size = batch size
        self.max_buffer_length = min((self.ni, self.batch_size * 8, 1000)) if self.augment else 0

        # Cache images
        if cache == "ram" and not self.check_cache_ram():
            cache = False
        self.ims_rgb, self.im_hw0_rgb, self.im_hw_rgb = [None] * self.ni, [None] * self.ni, [None] * self.ni
        self.npy_files_rgb = [Path(f).with_suffix(".npy") for f in self.im_files_rgb]
        self.ims_ir, self.im_hw0_ir, self.im_hw_ir = [None] * self.ni, [None] * self.ni, [None] * self.ni
        self.npy_files_ir = [Path(f).with_suffix(".npy") for f in self.im_files_ir]
        if cache:
            self.cache_images_rgb(cache)
            self.cache_images_ir(cache)

        # Transforms
        self.transforms = self.build_transforms(hyp=hyp)

    def get_img_files(self, img_path, prefix):
        """Read image files."""
        try:
            f = []  # image files
            for p in img_path if isinstance(img_path, list) else [img_path]:
                p = Path(p)  # os-agnostic
                if p.is_dir():  # dir
                    f += glob.glob(str(p / "**" / "*.*"), recursive=True)
                    # F = list(p.rglob('*.*'))  # pathlib
                elif p.is_file():  # file
                    with open(p) as t:
                        t = t.read().strip().splitlines()
                        parent = str(p.parent) + os.sep
                        f += [x.replace("./", parent) if x.startswith("./") else x for x in t]  # local to global path
                        # F += [p.parent / x.lstrip(os.sep) for x in t]  # local to global path (pathlib)
                else:
                    raise FileNotFoundError(f"{prefix}{p} does not exist")
            im_files = sorted(x.replace("/", os.sep) for x in f if x.split(".")[-1].lower() in IMG_FORMATS)
            # self.img_files = sorted([x for x in f if x.suffix[1:].lower() in IMG_FORMATS])  # pathlib
            assert im_files, f"{prefix}No images found in {img_path}"
        except Exception as e:
            raise FileNotFoundError(f"{prefix}Error loading data from {img_path}\n{HELP_URL}") from e
        if self.fraction < 1:
            im_files = im_files[: round(len(im_files) * self.fraction)]
        return im_files

    def update_labels(self, include_class: Optional[list]):
        """Update labels to include only these classes (optional)."""
        include_class_array = np.array(include_class).reshape(1, -1)
        for i in range(len(self.labels_rgb)):
            if include_class is not None:
                cls = self.labels_rgb[i]["cls"]
                bboxes = self.labels_rgb[i]["bboxes"]
                segments = self.labels_rgb[i]["segments"]
                keypoints = self.labels_rgb[i]["keypoints"]
                j = (cls == include_class_array).any(1)
                self.labels_rgb[i]["cls"] = cls[j]
                self.labels_rgb[i]["bboxes"] = bboxes[j]
                if segments:
                    self.labels_rgb[i]["segments"] = [segments[si] for si, idx in enumerate(j) if idx]
                if keypoints is not None:
                    self.labels_rgb[i]["keypoints"] = keypoints[j]
            if self.single_cls:
                self.labels_rgb[i]["cls"][:, 0] = 0
        for i in range(len(self.labels_ir)):
            if include_class is not None:
                cls = self.labels_ir[i]["cls"]
                bboxes = self.labels_ir[i]["bboxes"]
                segments = self.labels_ir[i]["segments"]
                keypoints = self.labels_ir[i]["keypoints"]
                j = (cls == include_class_array).any(1)
                self.labels_ir[i]["cls"] = cls[j]
                self.labels_ir[i]["bboxes"] = bboxes[j]
                if segments:
                    self.labels_ir[i]["segments"] = [segments[si] for si, idx in enumerate(j) if idx]
                if keypoints is not None:
                    self.labels_ir[i]["keypoints"] = keypoints[j]
            if self.single_cls:
                self.labels_ir[i]["cls"][:, 0] = 0

    def load_image_rgb(self, i, rect_mode=True):
        """Loads 1 image from dataset index 'i', returns (im, resized hw)."""
        im, f, fn = self.ims_rgb[i], self.im_files_rgb[i], self.npy_files_rgb[i]
        if im is None:  # not cached in RAM
            if fn.exists():  # load npy
                try:
                    im = np.load(fn)
                except Exception as e:
                    LOGGER.warning(f"{self.prefix_rgb}WARNING ⚠️ Removing corrupt *.npy image file {fn} due to: {e}")
                    Path(fn).unlink(missing_ok=True)
                    im = cv2.imread(f)  # BGR
            else:  # read image
                im = cv2.imread(f)  # BGR
            if im is None:
                raise FileNotFoundError(f"Image Not Found {f}")

            h0, w0 = im.shape[:2]  # orig hw
            if rect_mode:  # resize long side to imgsz while maintaining aspect ratio
                r = self.imgsz / max(h0, w0)  # ratio
                if r != 1:  # if sizes are not equal
                    w, h = (min(math.ceil(w0 * r), self.imgsz), min(math.ceil(h0 * r), self.imgsz))
                    im = cv2.resize(im, (w, h), interpolation=cv2.INTER_LINEAR)
            elif not (h0 == w0 == self.imgsz):  # resize by stretching image to square imgsz
                im = cv2.resize(im, (self.imgsz, self.imgsz), interpolation=cv2.INTER_LINEAR)

            # Add to buffer if training with augmentations
            if self.augment:
                self.ims_rgb[i], self.im_hw0_rgb[i], self.im_hw_rgb[i] = im, (h0, w0), im.shape[:2]  # im, hw_original, hw_resized
                self.buffer_rgb.append(i)
                if len(self.buffer_rgb) >= self.max_buffer_length:
                    j = self.buffer_rgb.pop(0)
                    self.ims_rgb[j], self.im_hw0_rgb[j], self.im_hw_rgb[j] = None, None, None

            return im, (h0, w0), im.shape[:2]

        return self.ims_rgb[i], self.im_hw0_rgb[i], self.im_hw_rgb[i]

    def cache_images_rgb(self, cache):
        """Cache images to memory or disk."""
        b, gb = 0, 1 << 30  # bytes of cached images, bytes per gigabytes
        fcn = self.cache_images_to_disk_rgb if cache == "disk" else self.load_image_rgb
        with ThreadPool(NUM_THREADS) as pool:
            results = pool.imap(fcn, range(self.ni))
            pbar = TQDM(enumerate(results), total=self.ni, disable=LOCAL_RANK > 0)
            for i, x in pbar:
                if cache == "disk":
                    b += self.npy_files_rgb[i].stat().st_size
                else:  # 'ram'
                    self.ims_rgb[i], self.im_hw0_rgb[i], self.im_hw_rgb[i] = x  # im, hw_orig, hw_resized = load_image(self, i)
                    b += self.ims_rgb[i].nbytes
                pbar.desc = f"{self.prefix_rgb}Caching images ({b / gb:.1f}GB {cache})"
            pbar.close()

    def cache_images_to_disk_rgb(self, i):
        """Saves an image as an *.npy file for faster loading."""
        f = self.npy_files_rgb[i]
        if not f.exists():
            np.save(f.as_posix(), cv2.imread(self.im_files_rgb[i]), allow_pickle=False)

    def load_image_ir(self, i, rect_mode=True):
        """Loads 1 image from dataset index 'i', returns (im, resized hw)."""
        im, f, fn = self.ims_ir[i], self.im_files_ir[i], self.npy_files_ir[i]
        if im is None:  # not cached in RAM
            if fn.exists():  # load npy
                try:
                    im = np.load(fn)
                except Exception as e:
                    LOGGER.warning(f"{self.prefix_ir}WARNING ⚠️ Removing corrupt *.npy image file {fn} due to: {e}")
                    Path(fn).unlink(missing_ok=True)
                    im = cv2.imread(f)  # BGR
            else:  # read image
                im = cv2.imread(f)  # BGR
            if im is None:
                raise FileNotFoundError(f"Image Not Found {f}")

            h0, w0 = im.shape[:2]  # orig hw
            if rect_mode:  # resize long side to imgsz while maintaining aspect ratio
                r = self.imgsz / max(h0, w0)  # ratio
                if r != 1:  # if sizes are not equal
                    w, h = (min(math.ceil(w0 * r), self.imgsz), min(math.ceil(h0 * r), self.imgsz))
                    im = cv2.resize(im, (w, h), interpolation=cv2.INTER_LINEAR)
            elif not (h0 == w0 == self.imgsz):  # resize by stretching image to square imgsz
                im = cv2.resize(im, (self.imgsz, self.imgsz), interpolation=cv2.INTER_LINEAR)

            # Add to buffer if training with augmentations
            if self.augment:
                self.ims_ir[i], self.im_hw0_ir[i], self.im_hw_ir[i] = im, (h0, w0), im.shape[:2]  # im, hw_original, hw_resized
                self.buffer_ir.append(i)
                if len(self.buffer_ir) >= self.max_buffer_length:
                    j = self.buffer_ir.pop(0)
                    self.ims_ir[j], self.im_hw0_ir[j], self.im_hw_ir[j] = None, None, None

            return im, (h0, w0), im.shape[:2]

        return self.ims_ir[i], self.im_hw0_ir[i], self.im_hw_ir[i]

    def cache_images_ir(self, cache):
        """Cache images to memory or disk."""
        b, gb = 0, 1 << 30  # bytes of cached images, bytes per gigabytes
        fcn = self.cache_images_to_disk_ir if cache == "disk" else self.load_image_ir
        with ThreadPool(NUM_THREADS) as pool:
            results = pool.imap(fcn, range(self.ni))
            pbar = TQDM(enumerate(results), total=self.ni, disable=LOCAL_RANK > 0)
            for i, x in pbar:
                if cache == "disk":
                    b += self.npy_files_ir[i].stat().st_size
                else:  # 'ram'
                    self.ims_ir[i], self.im_hw0_ir[i], self.im_hw_ir[i] = x  # im, hw_orig, hw_resized = load_image(self, i)
                    b += self.ims_ir[i].nbytes
                pbar.desc = f"{self.prefix_ir}Caching images ({b / gb:.1f}GB {cache})"
            pbar.close()

    def cache_images_to_disk_ir(self, i):
        """Saves an image as an *.npy file for faster loading."""
        f = self.npy_files_ir[i]
        if not f.exists():
            np.save(f.as_posix(), cv2.imread(self.im_files_ir[i]), allow_pickle=False)

    def check_cache_ram(self, safety_margin=0.5):
        """Check image caching requirements vs available memory."""
        b, gb = 0, 1 << 30  # bytes of cached images, bytes per gigabytes
        n = min(self.ni, 30)  # extrapolate from 30 random images
        for _ in range(n):
            im = cv2.imread(random.choice(self.im_files_rgb))  # sample image
            ratio = self.imgsz / max(im.shape[0], im.shape[1])  # max(h, w)  # ratio
            b += im.nbytes * ratio**2
        mem_required = b * self.ni / n * (1 + safety_margin)  # GB required to cache dataset into RAM
        mem = psutil.virtual_memory()
        cache = mem_required * 2 < mem.available  # to cache or not to cache, that is the question
        if not cache:
            LOGGER.info(
                f'{self.prefix_rgb}and{self.prefix_ir}{mem_required / gb:.1f}GB RAM required to cache images '
                f'with {int(safety_margin * 100)}% safety margin but only '
                f'{mem.available / gb:.1f}/{mem.total / gb:.1f}GB available, '
                f"{'caching images ✅' if cache else 'not caching images ⚠️'}"
            )
        return cache

    def set_rectangle(self):
        """Sets the shape of bounding boxes for YOLO detections as rectangles."""
        bi = np.floor(np.arange(self.ni) / self.batch_size).astype(int)  # batch index
        nb = bi[-1] + 1  # number of batches

        s = np.array([x.pop("shape") for x in self.labels_rgb])  # hw
        ar = s[:, 0] / s[:, 1]  # aspect ratio
        irect = ar.argsort()
        self.im_files_rgb = [self.im_files_rgb[i] for i in irect]
        self.labels_rgb = [self.labels_rgb[i] for i in irect]
        ar = ar[irect]

        s = np.array([x.pop("shape") for x in self.labels_ir])  # hw
        ar = s[:, 0] / s[:, 1]  # aspect ratio
        irect = ar.argsort()
        self.im_files_ir = [self.im_files_ir[i] for i in irect]
        self.labels_ir = [self.labels_ir[i] for i in irect]
        ar = ar[irect]

        # Set training image shapes
        shapes = [[1, 1]] * nb
        for i in range(nb):
            ari = ar[bi == i]
            mini, maxi = ari.min(), ari.max()
            if maxi < 1:
                shapes[i] = [maxi, 1]
            elif mini > 1:
                shapes[i] = [1, 1 / mini]

        self.batch_shapes = np.ceil(np.array(shapes) * self.imgsz / self.stride + self.pad).astype(int) * self.stride
        self.batch = bi  # batch index of image

    def __getitem__(self, index):
        """Returns transformed label information for given index."""
        return self.transforms(self.get_image_and_label(index))

    def get_image_and_label(self, index):
        """Get and return label information from the dataset."""
        label = deepcopy(self.labels_rgb[index])  # requires deepcopy() https://github.com/ultralytics/ultralytics/pull/1948
        label['im_file_ir'] = deepcopy(self.labels_ir[index]['im_file_ir'])
        label.pop("shape", None)  # shape is for rect, remove it
        label["img_rgb"], label["ori_shape"], label["resized_shape"] = self.load_image_rgb(index)
        label['img_ir'], _, _ = self.load_image_ir(index)
        label["ratio_pad"] = (
            label["resized_shape"][0] / label["ori_shape"][0],
            label["resized_shape"][1] / label["ori_shape"][1],
        )  # for evaluation
        if self.rect:
            label["rect_shape"] = self.batch_shapes[self.batch[index]]
        return self.update_labels_info(label)

    def __len__(self):
        """Returns the length of the labels list for the dataset."""
        return len(self.labels_rgb)

    def update_labels_info(self, label):
        """Custom your label format here."""
        return label

    def build_transforms(self, hyp=None):
        """
        Users can customize augmentations here.

        Example:
            ```python
            if self.augment:
                # Training transforms
                return Compose([])
            else:
                # Val transforms
                return Compose([])
            ```
        """
        raise NotImplementedError

    def get_labels_rgb(self):
        """
        Users can customize their own format here.

        Note:
            Ensure output is a dictionary with the following keys:
            ```python
            dict(
                im_file=im_file,
                shape=shape,  # format: (height, width)
                cls=cls,
                bboxes=bboxes, # xywh
                segments=segments,  # xy
                keypoints=keypoints, # xy
                normalized=True, # or False
                bbox_format="xyxy",  # or xywh, ltwh
            )
            ```
        """
        raise NotImplementedError
    
    def get_labels_ir(self):
        """
        Users can customize their own format here.

        Note:
            Ensure output is a dictionary with the following keys:
            ```python
            dict(
                im_file=im_file,
                shape=shape,  # format: (height, width)
                cls=cls,
                bboxes=bboxes, # xywh
                segments=segments,  # xy
                keypoints=keypoints, # xy
                normalized=True, # or False
                bbox_format="xyxy",  # or xywh, ltwh
            )
            ```
        """
        raise NotImplementedError