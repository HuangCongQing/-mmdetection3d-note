# Copyright (c) OpenMMLab. All rights reserved.
import mmcv
import numpy as np
import pickle
from mmcv import track_iter_progress
from mmcv.ops import roi_align
from os import path as osp
from pycocotools import mask as maskUtils
from pycocotools.coco import COCO

from mmdet3d.core.bbox import box_np_ops as box_np_ops
from mmdet3d.datasets import build_dataset
from mmdet.core.evaluation.bbox_overlaps import bbox_overlaps


def _poly2mask(mask_ann, img_h, img_w):
    if isinstance(mask_ann, list):
        # polygon -- a single object might consist of multiple parts
        # we merge all parts into one mask rle code
        rles = maskUtils.frPyObjects(mask_ann, img_h, img_w)
        rle = maskUtils.merge(rles)
    elif isinstance(mask_ann['counts'], list):
        # uncompressed RLE
        rle = maskUtils.frPyObjects(mask_ann, img_h, img_w)
    else:
        # rle
        rle = mask_ann
    mask = maskUtils.decode(rle)
    return mask


def _parse_coco_ann_info(ann_info):
    gt_bboxes = []
    gt_labels = []
    gt_bboxes_ignore = []
    gt_masks_ann = []

    for i, ann in enumerate(ann_info):
        if ann.get('ignore', False):
            continue
        x1, y1, w, h = ann['bbox']
        if ann['area'] <= 0:
            continue
        bbox = [x1, y1, x1 + w, y1 + h]
        if ann.get('iscrowd', False):
            gt_bboxes_ignore.append(bbox)
        else:
            gt_bboxes.append(bbox)
            gt_masks_ann.append(ann['segmentation'])

    if gt_bboxes:
        gt_bboxes = np.array(gt_bboxes, dtype=np.float32)
        gt_labels = np.array(gt_labels, dtype=np.int64)
    else:
        gt_bboxes = np.zeros((0, 4), dtype=np.float32)
        gt_labels = np.array([], dtype=np.int64)

    if gt_bboxes_ignore:
        gt_bboxes_ignore = np.array(gt_bboxes_ignore, dtype=np.float32)
    else:
        gt_bboxes_ignore = np.zeros((0, 4), dtype=np.float32)

    ann = dict(
        bboxes=gt_bboxes, bboxes_ignore=gt_bboxes_ignore, masks=gt_masks_ann)

    return ann


def crop_image_patch_v2(pos_proposals, pos_assigned_gt_inds, gt_masks):
    import torch
    from torch.nn.modules.utils import _pair
    device = pos_proposals.device
    num_pos = pos_proposals.size(0)
    fake_inds = (
        torch.arange(num_pos,
                     device=device).to(dtype=pos_proposals.dtype)[:, None])
    rois = torch.cat([fake_inds, pos_proposals], dim=1)  # Nx5
    mask_size = _pair(28)
    rois = rois.to(device=device)
    gt_masks_th = (
        torch.from_numpy(gt_masks).to(device).index_select(
            0, pos_assigned_gt_inds).to(dtype=rois.dtype))
    # Use RoIAlign could apparently accelerate the training (~0.1s/iter)
    targets = (
        roi_align(gt_masks_th, rois, mask_size[::-1], 1.0, 0, True).squeeze(1))
    return targets


def crop_image_patch(pos_proposals, gt_masks, pos_assigned_gt_inds, org_img):
    num_pos = pos_proposals.shape[0]
    masks = []
    img_patches = []
    for i in range(num_pos):
        gt_mask = gt_masks[pos_assigned_gt_inds[i]]
        bbox = pos_proposals[i, :].astype(np.int32)
        x1, y1, x2, y2 = bbox
        w = np.maximum(x2 - x1 + 1, 1)
        h = np.maximum(y2 - y1 + 1, 1)

        mask_patch = gt_mask[y1:y1 + h, x1:x1 + w]
        masked_img = gt_mask[..., None] * org_img
        img_patch = masked_img[y1:y1 + h, x1:x1 + w]

        img_patches.append(img_patch)
        masks.append(mask_patch)
    return img_patches, masks

# 数据增强：创建GT 数据集==================================================main入口   
 # 用trainfile的groundtruth产生groundtruth_database，
# 只保存训练数据中的gt_box及其包围的点的信息，用于数据增强
def create_groundtruth_database(dataset_class_name,
                                data_path,
                                info_prefix,
                                info_path=None, # './data/ouster/ouster_infos_train.pkl'训练集有label
                                mask_anno_path=None,
                                used_classes=None,
                                database_save_path=None, # 保存路径# './data/ouster/ouster_gt_database' 保存单个障碍物bin文件的文件夹
                                db_info_save_path=None, # './data/ouster/ouster_dbinfos_train.pkl'
                                relative_path=True,
                                add_rgb=False,
                                lidar_only=False,
                                bev_only=False,
                                coors_range=None,
                                with_mask=False):
    """Given the raw data, generate the ground truth database.

    Args:
        dataset_class_name （str): Name of the input dataset.
        data_path (str): Path of the data.
        info_prefix (str): Prefix of the info file.
        info_path (str): Path of the info file.   pkl文件data/kitti/kitti_dbinfos_train.pkl
            Default: None.
        mask_anno_path (str): Path of the mask_anno.
            Default: None.
        used_classes (list[str]): Classes have been used.
            Default: None.
        database_save_path (str): Path to save database.
            Default: None.
        db_info_save_path (str): Path to save db_info.
            Default: None.
        relative_path (bool): Whether to use relative path.
            Default: True.
        with_mask (bool): Whether to use mask.
            Default: False.
    """
    print(f'入数据增强Create GT Database of {dataset_class_name}')
    dataset_cfg = dict(
        type=dataset_class_name, data_root=data_path, ann_file=info_path)
    if dataset_class_name == 'KittiDataset':
        file_client_args = dict(backend='disk')
        dataset_cfg.update(
            test_mode=False,
            split='training',
            modality=dict(
                use_lidar=True,
                use_depth=False,
                use_lidar_intensity=True,
                use_camera=with_mask,
            ),
            pipeline=[ # ===============================================================================================================
                dict(
                    type='LoadPointsFromFile', # 
                    coord_type='LIDAR',
                    load_dim=4,
                    use_dim=4,
                    file_client_args=file_client_args),
                dict(
                    type='LoadAnnotations3D',
                    with_bbox_3d=True,
                    with_label_3d=True,
                    file_client_args=file_client_args)
            ])

    elif dataset_class_name == 'NuScenesDataset':
        dataset_cfg.update(
            use_valid_flag=True,
            pipeline=[
                dict(
                    type='LoadPointsFromFile',
                    coord_type='LIDAR',
                    load_dim=5,
                    use_dim=5),
                dict(
                    type='LoadPointsFromMultiSweeps',
                    sweeps_num=10,
                    use_dim=[0, 1, 2, 3, 4],
                    pad_empty_sweeps=True,
                    remove_close=True),
                dict(
                    type='LoadAnnotations3D',
                    with_bbox_3d=True,
                    with_label_3d=True)
            ])

    elif dataset_class_name == 'WaymoDataset':
        file_client_args = dict(backend='disk')
        dataset_cfg.update(
            test_mode=False,
            split='training',
            modality=dict(
                use_lidar=True,
                use_depth=False,
                use_lidar_intensity=True,
                use_camera=False,
            ),
            pipeline=[
                dict(
                    type='LoadPointsFromFile',
                    coord_type='LIDAR',
                    load_dim=6,
                    use_dim=5,
                    file_client_args=file_client_args),
                dict(
                    type='LoadAnnotations3D',
                    with_bbox_3d=True,
                    with_label_3d=True,
                    file_client_args=file_client_args)
            ])
    # Ouster新建=========================================================================================main
    elif dataset_class_name == 'OusterDataset':
        file_client_args = dict(backend='disk')
        # 配置
        dataset_cfg.update(
            test_mode=False,
            split='training',
            modality=dict(
                use_lidar=True,
                use_depth=False,
                use_lidar_intensity=True,
                use_camera=False, # 不用相机
            ),
            pipeline=[
                dict(
                    type='LoadPointsFromFile', # 加载点云文件
                    coord_type='LIDAR',
                    # load_dim=6,
                    load_dim=4, # ========================
                    use_dim=4,
                    file_client_args=file_client_args),
                dict(
                    type='LoadAnnotations3D', # 加载标注文件
                    with_bbox_3d=True,
                    with_label_3d=True,
                    file_client_args=file_client_args)
            ])

    dataset = build_dataset(dataset_cfg) # mmdet3d/datasets/builder.py!!!!!!!!!!!!？？？？？没运行到
    # 
    if database_save_path is None:
        database_save_path = osp.join(data_path, f'{info_prefix}_gt_database') # './data/ouster/ouster_gt_database' 保存单个障碍物bin文件的文件夹
    if db_info_save_path is None: # './data/kitti/kitti_dbinfos_train.pkl'
        db_info_save_path = osp.join(data_path,
                                     f'{info_prefix}_dbinfos_train.pkl') # './data/ouster/ouster_dbinfos_train.pkl'
    mmcv.mkdir_or_exist(database_save_path)
    all_db_infos = dict()
    # if with_mask: #  Default: False.
    #     coco = COCO(osp.join(data_path, mask_anno_path))
    #     imgIds = coco.getImgIds()
    #     file2id = dict()
    #     for i in imgIds:
    #         info = coco.loadImgs([i])[0]
    #         file2id.update({info['file_name']: i})

    group_counter = 0
    # 遍历》》》》
    for j in track_iter_progress(list(range(len(dataset)))):
        input_dict = dataset.get_data_info(j) # mmdet3d/datasets/kitti_dataset.py
        dataset.pre_pipeline(input_dict) #  2. 调用 pre_pipeline() ， 扩展 input_dict 包含的属性信息 mmdet3d/datasets/custom_3d.py
        example = dataset.pipeline(input_dict) # 报错位置 self.pipeline = Compose(pipeline)  mmdet3d/datasets/custom_3d.py将得到的===============================================================
        # 读取注释信息
        annos = example['ann_info']
        image_idx = example['sample_idx'] #'000003' image_idx来源  mage_idx: object 所在样本下标
        points = example['points'].tensor.numpy() # （65536， 4）原始点
        gt_boxes_3d = annos['gt_bboxes_3d'].tensor.numpy() # # GT点 7
        # name的数据是['car','car','pedestrian'...'dontcare'...]表示当前帧里面的所有物体objects
        names = annos['gt_names']
        group_dict = dict()
        if 'group_ids' in annos:
            group_ids = annos['group_ids'] #     group_id: object 属于该 group 里面的第几个 (也就是 list 的1-based index.)
        else:
            group_ids = np.arange(gt_boxes_3d.shape[0], dtype=np.int64)
        difficulty = np.zeros(gt_boxes_3d.shape[0], dtype=np.int32)
        if 'difficulty' in annos: # 没有
            difficulty = annos['difficulty']

        # num_obj是有效物体的个数，为N
        num_obj = gt_boxes_3d.shape[0] # 数量=======================================
        # 返回每个box中的点云索引[0 0 0 1 0 1 1...]
        if dataset_class_name == 'OusterDataset':
            point_indices = box_np_ops.points_in_rbbox(points, gt_boxes_3d, z_axis=2, origin=(0.5, 0.5, 0.5)) # 'mmdet3d.core.bbox.box_np_ops' has no attribute 'points_in_rbbox_ouster'
            # point_indices = box_np_ops.points_in_rbbox_ouster(points, gt_boxes_3d) # 取出box的中点云下标重要函数！！！！！！！
        else:
            point_indices = box_np_ops.points_in_rbbox(points, gt_boxes_3d, z_axis=2, origin=(0.5, 0.5, 0)) # 取出box的中点云下标重要函数！！！！！！！


        # if with_mask:
        #     # prepare masks
        #     gt_boxes = annos['gt_bboxes']
        #     img_path = osp.split(example['img_info']['filename'])[-1]
        #     if img_path not in file2id.keys():
        #         print(f'skip image {img_path} for empty mask')
        #         continue
        #     img_id = file2id[img_path]
        #     kins_annIds = coco.getAnnIds(imgIds=img_id)
        #     kins_raw_info = coco.loadAnns(kins_annIds)
        #     kins_ann_info = _parse_coco_ann_info(kins_raw_info)
        #     h, w = annos['img_shape'][:2]
        #     gt_masks = [
        #         _poly2mask(mask, h, w) for mask in kins_ann_info['masks']
        #     ]
        #     # get mask inds based on iou mapping
        #     bbox_iou = bbox_overlaps(kins_ann_info['bboxes'], gt_boxes)
        #     mask_inds = bbox_iou.argmax(axis=0)
        #     valid_inds = (bbox_iou.max(axis=0) > 0.5)
        #
        #     # mask the image
        #     # use more precise crop when it is ready
        #     # object_img_patches = np.ascontiguousarray(
        #     #     np.stack(object_img_patches, axis=0).transpose(0, 3, 1, 2))
        #     # crop image patches using roi_align
        #     # object_img_patches = crop_image_patch_v2(
        #     #     torch.Tensor(gt_boxes),
        #     #     torch.Tensor(mask_inds).long(), object_img_patches)
        #     object_img_patches, object_masks = crop_image_patch(
        #         gt_boxes, gt_masks, mask_inds, annos['img'])

        # 遍历障碍物数量
        for i in range(num_obj):
            ## 创建文件名，并设置保存路径，最后文件如：  (000003_Car_0.bin)
            filename = f'{image_idx}_{names[i]}_{i}.bin' # bin文件
            #
            abs_filepath = osp.join(database_save_path, filename) # 真正保存路径
            # /data/kitti/ouster_gt_database/000007_Cyclist_3.bin
            rel_filepath = osp.join(f'{info_prefix}_gt_database', filename)

            # save point clouds and image patches for each object
            # point_indices[i] > 0得到的是一个[T,F,T,T,F...]之类的真假索引，共有M个
            # 再从points中取出相应为true的点云数据，放在gt_points中
            gt_points = points[point_indices[:, i]] # 取出里面的点云（730， 4）
            # 将第i个box内点转化为局部坐标
            gt_points[:, :3] -= gt_boxes_3d[i, :3] # GT点 x,y,z,

            # if with_mask: # 不执行吧
            #     if object_masks[i].sum() == 0 or not valid_inds[i]:
            #         # Skip object for empty or invalid mask
            #         continue
            #     img_patch_path = abs_filepath + '.png' # 图像
            #     mask_patch_path = abs_filepath + '.mask.png'
            #     mmcv.imwrite(object_img_patches[i], img_patch_path)
            #     mmcv.imwrite(object_masks[i], mask_patch_path)

            # # 把gt_points的信息写入文件里 保存bin文件！！！！！！！
            # './data/ouster/ouster_gt_database/4_Truck_0.bin'
            with open(abs_filepath, 'w') as f:
                print("\n gt_points的shape: ", gt_points.shape ,"保存路径", abs_filepath)
                gt_points.tofile(f)

            if (used_classes is None) or names[i] in used_classes:
                db_info = {
                    'name': names[i],
                    'path': rel_filepath,
                    'image_idx': image_idx, #      image_idx: object 所在样本下标
                    'gt_idx': i,     #  gt_idx: object 属于该样本的第几个 object
                    'box3d_lidar': gt_boxes_3d[i], #      group_id: object 属于该 group 里面的第几个 (也就是 list 的1-based index.)
                    'num_points_in_gt': gt_points.shape[0],
                    'difficulty': difficulty[i],
                }
                local_group_id = group_ids[i]
                # if local_group_id >= 0:
                if local_group_id not in group_dict:
                    group_dict[local_group_id] = group_counter
                    group_counter += 1
                db_info['group_id'] = group_dict[local_group_id]
                if 'score' in annos:
                    db_info['score'] = annos['score'][i]
                # if with_mask: # 不执行吧
                #     db_info.update({'box2d_camera': gt_boxes[i]})
                if names[i] in all_db_infos:
                    all_db_infos[names[i]].append(db_info) # 统一保存在
                else:
                    all_db_infos[names[i]] = [db_info]

    for k, v in all_db_infos.items():
        print(f'加载load {len(v)} {k} database infos')
        # 加载load 96 Truck database infos

    with open(db_info_save_path, 'wb') as f:
        pickle.dump(all_db_infos, f) # 生成pkl数据
