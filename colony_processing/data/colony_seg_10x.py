import math
from aicsimageio import AICSImage
# from interactive_location_picker_pyqtgraph import \
#     ImageLocationPicker # Module to implement selecting points on an image
from matplotlib.pyplot import imshow, show, rcParams
import numpy
# from pyqtgraph.Qt import QtGui
from scipy import ndimage, signal
from skimage import exposure, feature, morphology, transform, filters, measure, color
import matplotlib.pyplot as plt

DOWNSCALING_FACTOR = 4
rcParams['figure.figsize'] = 15, 12


class WellSegmentation:

    def __init__(self, image_data, colony_filters_dict=None, mode='A', canny_sigma=0.01, canny_low_threshold=0.025,
                 remove_small_holes_area_threshold=1000):
        """
        :param
        image_data =  numpy image data Image to be segmented (Well or colony)
        filters_dict = dictionary of filters to be applied { filter_name: filter_values}
        """
        self._point_locations = []
        self.segmented_colonies = None
        # Downscale the colony dictionary
        self.colony_filters_dict = self.downscale_filter_dictionary(colony_filters_dict)
        # Downsize the image
        xdim = image_data.shape[0]
        ydim = image_data.shape[1]
        self.downsized_image = transform.resize(image_data, (int(xdim / DOWNSCALING_FACTOR), int(ydim / DOWNSCALING_FACTOR)),
                                           mode='constant')
        self.height, self.width = self.downsized_image.shape[:2]

    def downscale_filter_dictionary(self, colony_filters_dict):
        """
        To downscale filters from original image to processing image scale
        :param colony_filters_dict: dictionary of filters with colony_filters_dict['distFromCenter'][0] as distance from
        center and colony_filters_dict['distFromCenter'][1] as number of positions
        :return: A corrected colony filteres dictionary with downscaling factor
        """
        colony_filters_dict_corrected = {}
        colony_filters_dict_corrected['distFromCenter'] = [
            colony_filters_dict['distFromCenter'][0] / DOWNSCALING_FACTOR,
            colony_filters_dict['distFromCenter'][1]]
        colony_filters_dict_corrected['minArea'] = []
        for value in colony_filters_dict['minArea']:
            colony_filters_dict_corrected['minArea'].append(value / (DOWNSCALING_FACTOR ** 2))
        return colony_filters_dict_corrected

    @property
    def point_locations(self):
        return self._point_locations

    def segment_and_find_positions(self):
        """
        Function segments out the colonies, applied filters and find the smooth points based on the mode
        :return:
        """
        rescaled_image = self.preprocessing_image()
        binary_colony_mask, well_mask = self.segment_colonies(rescaled_image)

        # label_ws = measure.label(binary_colony_mask)


        self.process_colonies(binary_colony_mask)

        seg_overlay = color.label2rgb(self.segmented_colonies, image=rescaled_image, bg_label=0)
        # self.find_positions()

        return self.segmented_colonies, seg_overlay, well_mask

    def preprocessing_image(self):
        """
        To pre-process input image with correction for uneven illumination and rescaling intensity
        to enhance contrast
        :return: A rescaled image ready for segmentation
        """

        rescaled_image = exposure.rescale_intensity(self.downsized_image)
        # Correct for uneven illumination in a well
        blur = filters.gaussian(rescaled_image, sigma=1, preserve_range=True)
        correction = self.downsized_image - blur
        # To mask out edges of well and only process colonies from center of well
        mask = self.create_circular_mask(radius=(self.height / 2.1) - 5.)
        masked_image = correction.copy()
        # masked_image[~mask] = 0
        # Smooth and rescale image to enhance contrast before filtering to 5th and 95th percentile of pixel intensity
        p5, p95 = numpy.percentile(masked_image, (5, 95))
        img_rescale = exposure.rescale_intensity(masked_image, in_range=(p5, p95))
        gaussian = filters.gaussian(img_rescale, sigma=0.5, preserve_range=True)
        p5, p95 = numpy.percentile(gaussian, (5, 95))
        img_rescale_2 = exposure.rescale_intensity(gaussian, in_range=(p5, p95))
        print ('Completed image-preprocessing')
        return img_rescale_2

    def segment_colonies(self, rescaled_image):
        """
        To segment colonies from grayscale image using edge detection method to approximate location
        of colony edges and watershed segmentation to fill in the colonies
        :param rescaled_image: The rescaled image from preprocessing with enhanced contrast
        :return: A binary image of mask of colonies(1) and background(0)
        """
        # Create the cell colony outline for watershed
        # Apply sobel filter to highlight edges
        sobel_0 = filters.sobel(rescaled_image)
        # Smooth out sobel edges to highlight
        gaussian_sobel = filters.gaussian(sobel_0, sigma=5, preserve_range=True)
        # Apply sobel filter on smoothed image to highlight edge of a cell colony
        sobel = filters.sobel(gaussian_sobel)
        # Set threshold of edges of colony
        contours_test = sobel
        countour_cutoff = 0.015
        contours_test[contours_test >= countour_cutoff] = 1
        contours_test[contours_test < countour_cutoff] = 0
        # Apply a mask to remove edge of the well
        mask_2 = self.create_circular_mask(radius=(self.height / 2.1) - 20.)

        contours_masked = contours_test.copy()
        # contours_masked[~mask_2] = 0
        # Adjust morphology of edge of a cell colony to reach 'closing'
        filtered = self.filter_small_objects(contours_masked, 300)
        erosion = morphology.erosion(filtered, morphology.diamond(3))
        closing = morphology.binary_closing(erosion, selem=morphology.diamond(5))
        remove = self.filter_small_objects(closing, 300)
        colony_edge = morphology.binary_closing(remove, selem=morphology.disk(5))
        print ('Created colony edges for binary colony mask')

        # Create cell colony markers for watershed
        # Compute intensity profile from gaussian_sobel
        frq, bin_edges = numpy.histogram(gaussian_sobel, bins=64)
        # Identify peaks in intensity profile to separate background from true signal
        # with limiting the distance between two peaks be 10 or more
        peaks, dictionary = signal.find_peaks(frq, distance=5)

        # Calculate background (peak to the left) and signal (peak to the right) thresholds
        if len(peaks)>=2:
            # Find the two highest peaks that reference to background or signal
            order = frq[peaks].argsort()
            ranks = order.argsort()
            highest_peak = numpy.where(ranks == numpy.amax(ranks))
            second_peak = numpy.where(ranks == (numpy.amax(ranks) - 1))
            diff = math.fabs(bin_edges[peaks[second_peak[0]]][0] - bin_edges[peaks[highest_peak[0]]][0])
            if peaks[highest_peak[0]] < peaks[second_peak[0]]:
                sig_peak = bin_edges[peaks[second_peak[0]]] - diff * 0.5
                back_peak = bin_edges[peaks[highest_peak[0]]] + diff * 0.15
            else:
                sig_peak = bin_edges[peaks[highest_peak[0]]] - diff * 0.5
                back_peak = bin_edges[peaks[second_peak[0]]] + diff * 0.15
        elif len(peaks)==1:
            back_peak = bin_edges[peaks[0]]*0.9
            sig_peak = bin_edges[peaks[0]]*3
        else:
            back_peak = np.mean(gaussian_sobel)*0.5
            sig_peak = np.mean(gaussian_sobel)*3

        # Create markers for watershed
        mask_sobel = gaussian_sobel.copy()
        mask_sobel[~mask_2] = 0
        # Apply thresholds to markers
        markers = numpy.zeros_like(mask_sobel)
        markers[mask_sobel < back_peak] = 1
        markers[mask_sobel > sig_peak] = 2
        # Adjust morphology of markers
        objects = numpy.zeros(markers.shape)
        objects[markers == 2] = 1
        erode_objects = morphology.erosion(objects, morphology.disk(3))
        remove_small = self.filter_small_objects(erode_objects, area=500)
        erode_objects = morphology.erosion(remove_small, morphology.disk(5))
        # Difference between objects and erode_objects could be holes in a colony
        # that should be set as '0', neither background(1) or colony(2)
        diff = objects - erode_objects
        markers[diff == 1] = 0
        print ('Created markers for binary colony mask')

        # --------------------------------------------------------------------------
        # Apply watershed segmentation
        segmentation = morphology.watershed(colony_edge, markers)
        segmentation[segmentation == 1] = 0.
        segmentation[segmentation == 2] = 1.

        # Adjust morphology of cell colonies segmented
        binary_colony_mask = morphology.erosion(segmentation, morphology.disk(5))
        print ('Completed creating binary colony mask')
        return binary_colony_mask, mask_2


    def process_colonies(self, binary_colony_mask):
        """
        To partition binary colony mask into separate, distinct, labelled colonies using
        distance map to find markers of colonies and separate connected colonies with watershed
        segmentation.
        :param binary_colony_mask: A binary image of colonies(1) and background (0)
        :return: An image of labelled, separate colonies
        """
        # --------------------------------------------------------------------------
        # Colony partition
        # Create distance map and 'cut' weak bondings between separating colonies
        erode = morphology.erosion(binary_colony_mask, morphology.disk(20))
        distance_map = ndimage.morphology.distance_transform_edt(erode)
        distance_map[distance_map < 10] = 0

        # Create distance map for all colonies
        distance_map_max = ndimage.morphology.distance_transform_edt(binary_colony_mask)

        # Identify local maximum from filtered distance map and
        # use maximum as markers for watershed. This method will
        # identify centers of big colonies
        local_maxi = feature.peak_local_max(distance_map, indices=False, footprint=numpy.ones((200, 200)))
        dilate_maxi = morphology.dilation(local_maxi, selem=morphology.disk(10))

        dis_obj = numpy.zeros(distance_map.shape)
        dis_obj[distance_map > 0] = 1
        dilate_dis_obj = morphology.dilation(dis_obj, selem=morphology.disk(10))
        lab_dis_obj = measure.label(dilate_dis_obj)

        union_obj = numpy.unique(lab_dis_obj[numpy.where((dilate_maxi > 0) & (lab_dis_obj > 0))])
        obj_to_add = []
        for obj in numpy.unique(lab_dis_obj):
            if (obj not in union_obj) & (obj > 0):
                obj_to_add.append(obj)
                dilate_maxi[lab_dis_obj == obj] = 1

        # As overall colony segmentation was not able to separate colonies that are close to each other
        # and tend to merge neighboring colonies, a secondary filter is applied to the distance map
        # to pick up signals from small colonies to find center of small colonies and add to markers
        # for watershed
        filter_max = distance_map_max.copy()
        filter_max[filter_max < 10] = 0
        filter_max[filter_max > 0] = 1

        remove_small = self.filter_small_objects(filter_max, area=2500)
        small_obj = filter_max - remove_small
        final_small = morphology.dilation(small_obj, selem=morphology.disk(5))

        # Merge markers from big and small colonies
        total = numpy.logical_or(dilate_maxi, final_small)
        markers = measure.label(total)
        # Apply watershed segmentation
        labelled_colonies = morphology.watershed(-distance_map_max, markers, mask=binary_colony_mask)
        print ('Identified and labelled separate colonies from binary colony mask')

        # Partition big colonies into few smaller ones
        sizes = numpy.bincount(labelled_colonies.ravel())
        # Set minimum colony size to be split
        big_obj_to_split = []
        for i in range(1, len(sizes)):
            if sizes[i] > 90000:
                big_obj_to_split.append(i)

        mask = numpy.zeros(labelled_colonies.shape)
        for obj in big_obj_to_split:
            mask[labelled_colonies == obj] = 1
        # Apply distance mapping to the large colony and try to separate the colony
        # by finding peaks in the distance map
        dis_map = ndimage.morphology.distance_transform_edt(mask)
        local_maxi = feature.peak_local_max(dis_map, indices=False, footprint=numpy.ones((100, 100)))
        dilate = morphology.dilation(local_maxi, selem=morphology.disk(10))
        seed = measure.label(dilate)
        # Apply watershed segmentation on the colony with new seeds to partition
        split = morphology.watershed(-dis_map, seed, mask=mask)
        print ('Partitioned big colonies to smaller colonies')

        # Adjust labeling with new partitioned colonies
        for obj in range(1, len(numpy.unique(split))):
            labelled_colonies[split == obj] = numpy.max(labelled_colonies) + 1
        self.segmented_colonies = labelled_colonies
        print("Done Segmenting colonies")

    def find_positions(self):
        """
        To find a position in a colony that passes the size filter, and is positioned 40% from
        the edge of colony, maximum in distance map that indicates preferred smoothness in region,
        and is close to the center of the well for good imaging practice
        :return: A list of positions that pass filters and rankings
        """
        # Filter colony by size
        min_area = self.colony_filters_dict['minArea'][0]
        # Calculating the sizes of the object
        sizes = numpy.bincount(self.segmented_colonies.ravel())
        # Remove background size
        sizes_colony = numpy.delete(sizes, 0)

        # Selecting objects above a certain size threshold
        size_mask = sizes_colony > min_area
        obj_number_keep = numpy.where(size_mask == True)[0]
        num_colonies_final = self.colony_filters_dict['distFromCenter'][1]

        filtered = numpy.zeros(self.segmented_colonies.shape)

        # TODO  - Test for 0 position
        # If there is equal or less # colonies segmented than wanted, use all colonies for picking positions
        # If there is less # colonies segmented than wanted, flag user that colonies are small in the well
        # and use the largest colonies to generate positions
        # If there are more colonies segmented than wanted, use the largest #wanted+2 colonies (+2 gives the
        # flexibility later to pick positions that are from a slightly smaller colony but closer to well center)

        if len(obj_number_keep) <= num_colonies_final:
            for obj in obj_number_keep:
                filtered[numpy.where(self.segmented_colonies == obj + 1)] = obj + 1
            num_objs = len(obj_number_keep)
            if len(obj_number_keep) < num_colonies_final:
                print('small colonies in this well')
                # Get colonies of largest size
                desc_rank = numpy.argsort(-sizes_colony)
                size_index = desc_rank[:num_colonies_final]
                num_obj = 1
                for obj in size_index:
                    filtered[numpy.where(self.segmented_colonies == obj + 1)] = num_obj
                    num_obj += 1
                num_objs = num_obj - 1

        else:
            desc_rank = numpy.argsort(-sizes_colony)
            size_index = desc_rank[:(num_colonies_final + 2)]
            num_obj = 1
            for obj in size_index:
                filtered[numpy.where(self.segmented_colonies == obj + 1)] = num_obj
                num_obj += 1
            num_objs = num_obj - 1

        filtered_colonies = measure.label(filtered)
        print ('Filtered colonies according to size')
        # Select 1 position per colony and populate the point location in original-sized image in [point_locations]
        # with distance map
        smoothed_well = ndimage.gaussian_filter(self.downsized_image, 0.35)
        distance = ndimage.distance_transform_edt(filtered_colonies)

        point_locations = []
        for obj in range(1, num_objs + 1):
            print("On object {} of {}".format(obj, num_objs))
            mask = filtered_colonies== obj
            dist_mask = distance * mask
            # for each colony, find the maximum distance from the two fold distance map.
            # The edge is at 0% and the center of the colony is at 100%
            d_max = dist_mask.max()
            # Getting the points which is at least 40% away from the edge
            top_percent = dist_mask > (d_max * 0.30)
            colony_mask = smoothed_well * top_percent
            colony_edges = feature.canny(colony_mask, sigma=0.1)
            # applying the second distance transform to find the smoothest point in the correct region
            inner_edges = ndimage.distance_transform_edt(~colony_edges * top_percent)
            smooth_point = numpy.where(inner_edges == inner_edges.max())
            smooth_point = (smooth_point[0][0], smooth_point[1][0])
            smooth_point_corrected = (smooth_point[0] * DOWNSCALING_FACTOR, smooth_point[1] * DOWNSCALING_FACTOR)
            point_locations.append(smooth_point_corrected)
        print ('Calculated point distances from center of well')
        # Filter top point locations that are closest to the center of the well
        center_well_y = (self.height / 2) * DOWNSCALING_FACTOR
        center_well_x = (self.width / 2 ) * DOWNSCALING_FACTOR
        diff_center = []
        for location in point_locations:
            y = location[0]
            x = location[1]
            # Calculate distance of point from center
            diff = math.sqrt((x - center_well_x) ** 2 + (y - center_well_y) ** 2)
            diff_center.append(diff)
        # Rank the distance of point from center
        rank = numpy.argsort(diff_center)
        # Select the # points wanted with points closest to the center
        rank_index = rank[:num_colonies_final]
        for point in rank_index:
            self.point_locations.append(point_locations[point])
        print ('Ranked and picked ' + str(num_colonies_final) + ' points closest to center of well')



    def create_circular_mask(self, center=None, radius=None):
        """
        To create a circular mask over an image, masking out edges of a well
        :param center: user-defined center of circular mask
        :param radius: radius of circular mask
        :return: A mask with masked-out area be 0, and in-mask area be 1
        """
        if center is None:  # use the middle of the image
            center = [int(self.width / 2), int(self.height / 2)]
        if radius is None:  # use the smallest distance between the center and image walls
            radius = min(center[0], center[1], self.width - center[0], self.height - center[1])

        Y, X = numpy.ogrid[:self.height, :self.width]
        dist_from_center = numpy.sqrt((X - center[0]) ** 2 + (Y - center[1]) ** 2)

        mask = dist_from_center <= radius
        return mask

    def filter_small_objects(self, bw_img, area):
        """
        To filter small objects from image
        :param bw_img: A binary image with objects
        :param area: Objects smaller than this area will be filtered out
        :return: A binary image with objects smaller than specified area be filtered out
        """
        label_objects, nb_labels = ndimage.label(bw_img)
        sizes = numpy.bincount(label_objects.ravel())
        max_area = max(sizes)
        # Selecting objects above a certain size threshold
        # size_mask = (sizes > area) & (sizes < max_area)
        size_mask = (sizes > area)
        size_mask[0] = 0
        filtered = label_objects.copy()
        filtered_image = size_mask[filtered]

        int_img = numpy.zeros(filtered_image.shape)
        int_img[filtered_image == True] = 1
        int_img = int_img.astype(int)
        return int_img

import os
from skimage import io, measure
import numpy as np
import pandas as pd
from scipy.spatial import distance

output_folder = '/allen/aics/microscopy/Data/fov_in_colony/colony_seg'

dataset_dict = {
    'AICS-12': [4, 86],
    'AICS-16': [6],
    'AICS-24': [43, 89],
    'AICS-7': [5],
    'AICS-46': [],
    'AICS-54': [25],
    'AICS-13': [12],
    'AICS-14': [11],
    'AICS-57': [44],
    'AICS-61': [83],
    'AICS-69': [84],
    'AICS-11': [3, 87],
    'AICS-10': [14, 88],
    'AICS-25': [15],
    'AICS-22': [2, 90],
    'AICS-33': [40],
    'AICS-40': [41],
    'AICS-32': [8],
    'AICS-17': [10],
    'AICS-23': [16],
    'AICS-53': [9],
    'AICS-5': [35],
    'AICS-58': [7]
}

csv_folder = '/allen/aics/microscopy/Data/fov_in_colony'
csv_paths = []
for cell_line, datasets in dataset_dict.items():
    for dataset_id in datasets:
        csv_paths.append(csv_folder + '/' + cell_line + '_' + str(dataset_id) + '_10x_meta.csv')

if __name__ == "__main__":

    for csv in csv_paths:
        df_per_colony = pd.DataFrame()
        df_per_well = pd.DataFrame()
        df = pd.read_csv(csv)
        cell_line = csv.split('AICS-')[1][0:2]
        dataset_id = csv.split('_')[-3]

        for index, row in df.iterrows():
            im_path = row['stitched_path'].replace('\\', '/')
            image = row['filename_10x']
            img = AICSImage(im_path)
            colony_filters = {
                'minArea': [200000],  # min & max area of the colony
                'distFromCenter': [1600, 4]  # max distance from center & the number of colonies you want
                }
            seg = WellSegmentation(img.data[0, 0, 0, 0],
                                   colony_filters_dict=colony_filters, mode='A')
            label_ws, overlay, well_mask = seg.segment_and_find_positions()

            file_name = image.replace('.czi', '_seg.tiff')
            io.imsave(os.path.join(output_folder, file_name), label_ws)
            io.imsave(os.path.join(output_folder, image.replace('.czi', '_overlay.png')), overlay)

            area_mask = np.sum(well_mask)
            well_center = (label_ws.shape[0] / 2, label_ws.shape[1] / 2)

            if label_ws.max() >= 1:
                colony_props = pd.DataFrame(measure.regionprops_table(label_ws, properties=['label', 'area', 'centroid']))
                colonies_in_well = pd.DataFrame()
                for index, row in colony_props.iterrows():
                    colony_center = (colony_props.loc[index, 'centroid-0'], colony_props.loc[index, 'centroid-1'])
                    dist = distance.euclidean(well_center, colony_center)
                    output = {'file_name': image,
                              'colony_num': row['label'],
                              'area_colony': row['area'],
                              'centroid_colony': colony_center,
                              'dist_center': dist}
                    df_per_colony = df_per_colony.append(output, ignore_index=True)
                    colonies_in_well = colonies_in_well.append(output, ignore_index=True)

                median_colony_area = np.median(colonies_in_well['area_colony'].values.tolist())
                std_colony_area = np.mean(colonies_in_well['area_colony'].values.tolist())
                min_colony_area = np.min(colonies_in_well['area_colony'].values.tolist())
                max_colony_area = np.max(colonies_in_well['area_colony'].values.tolist())
                mean_colony_area = np.mean(colonies_in_well['area_colony'].values.tolist())
                sum_colony_area = np.sum(colonies_in_well['area_colony'].values.tolist())

                median_dist = np.median(colonies_in_well['dist_center'].values.tolist())
                mean_dist = np.mean(colonies_in_well['dist_center'].values.tolist())
                min_dist = np.min(colonies_in_well['dist_center'].values.tolist())
                max_dist = np.max(colonies_in_well['dist_center'].values.tolist())
                std_dist = np.std(colonies_in_well['dist_center'].values.tolist())

                well_confluency = sum_colony_area / area_mask

                row_per_well = {'file_name': image,
                                'confluency': well_confluency,
                                'median_colony_area': median_colony_area,
                                'std_colony_area': std_colony_area,
                                'min_colony_area': min_colony_area,
                                'max_colony_area': max_colony_area,
                                'mean_colony_area': mean_colony_area,
                                'sum_colony_area': sum_colony_area,
                                'num_colonies': np.max(label_ws),
                                'median_dist': median_dist,
                                'mean_dist': mean_dist,
                                'min_dist': min_dist,
                                'max_dist': max_dist,
                                'std_dist': std_dist}

                df_per_well = df_per_well.append(row_per_well, ignore_index=True)
            else:
                row_per_well = {'file_name': image,
                                'confluency': 0,
                                'median_colony_area': 0,
                                'std_colony_area': 0,
                                'min_colony_area': 0,
                                'max_colony_area': 0,
                                'mean_colony_area': 0,
                                'sum_colony_area': 0,
                                'num_colonies': 0,
                                'median_dist': 0,
                                'mean_dist': 0,
                                'min_dist': 0,
                                'max_dist': 0,
                                'std_dist': 0}
                df_per_well = df_per_well.append(row_per_well, ignore_index=True)

        df_per_colony.to_csv(os.path.join(output_folder, 'output_per_colony_' + cell_line + '_' + str(dataset_id) + '.csv'))
        df_per_well.to_csv(os.path.join(output_folder, 'output_per_well_' + cell_line + str(dataset_id) + '.csv'))
