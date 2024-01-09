### Julian Schilliger - ThaumatoAnakalyptor - Vesuvius Challenge 2023

from rendering_utils.interpolate_image_3d import extract_from_image_3d, insert_into_image_3d
from rendering_utils.ppmparser import PPMParser
import argparse
from tqdm import tqdm

import os
import tifffile

import numpy as np
import torch

from concurrent.futures import ThreadPoolExecutor, as_completed
# nr threads
import multiprocessing


def load_grid(path_template, cords, grid_block_size=500, cell_block_size=500, uint8=True):
    """
    path_template: Template for the path to load individual grid files
    cords: Tuple (x, y, z) representing the corner coordinates of the grid block
    grid_block_size: Size of the grid block
    cell_block_size: Size of the individual grid files
    """
    # make grid_block_size an array with 3 elements
    if isinstance(grid_block_size, int):
        grid_block_size = np.array([grid_block_size, grid_block_size, grid_block_size])
    
    # Convert corner coordinates to file indices and generate the file path
    # Starting indices
    file_x_start, file_y_start, file_z_start = cords[0]//cell_block_size, cords[1]//cell_block_size, cords[2]//cell_block_size
    # Ending indices
    file_x_end, file_y_end, file_z_end = (cords[0] + grid_block_size[0])//cell_block_size, (cords[1] + grid_block_size[1])//cell_block_size, (cords[2] + grid_block_size[2])//cell_block_size

    # Generate the grid block
    if uint8:
        grid_block = np.zeros((grid_block_size[2], grid_block_size[0], grid_block_size[1]), dtype=np.uint8)
    else:
        grid_block = np.zeros((grid_block_size[2], grid_block_size[0], grid_block_size[1]), dtype=np.uint16)

    # Load the grid block from the individual grid files and place it in the larger grid block
    for file_x in range(file_x_start, file_x_end + 1):
        for file_y in range(file_y_start, file_y_end + 1):
            for file_z in range(file_z_start, file_z_end + 1):
                path = path_template.format(file_x, file_y, file_z)

                # Check if the file exists
                if not os.path.exists(path):
                    # print(f"File {path} does not exist.")
                    continue

                # Read the image
                with tifffile.TiffFile(path) as tif:
                    images = tif.asarray()

                if uint8:
                    images = np.uint8(images//256)

                # grid block slice position for the current file
                x_start = max(file_x*cell_block_size, cords[0])
                x_end = min((file_x + 1) * cell_block_size, cords[0] + grid_block_size[0])
                y_start = max(file_y*cell_block_size, cords[1])
                y_end = min((file_y + 1) * cell_block_size, cords[1] + grid_block_size[1])
                z_start = max(file_z*cell_block_size, cords[2])
                z_end = min((file_z + 1) * cell_block_size, cords[2] + grid_block_size[2])

                # Place the current file in the grid block
                try:
                    grid_block[z_start - cords[2]:z_end - cords[2], x_start - cords[0]:x_end - cords[0], y_start - cords[1]:y_end - cords[1]] = images[z_start - file_z*cell_block_size: z_end - file_z*cell_block_size, x_start - file_x*cell_block_size: x_end - file_x*cell_block_size, y_start - file_y*cell_block_size: y_end - file_y*cell_block_size]
                except:
                    print(f"Error in grid block placement for grid block {cords} and file {file_x}, {file_y}, {file_z}")

    return grid_block


def load_ppm_cubes(path, cube_size=500):
    with PPMParser(path).open() as ppm:
        im_shape = ppm.im_shape()
        cubes = ppm.classify_entries_to_cubes(cube_size=cube_size)
    return cubes, im_shape

def cube_coords(cube_key, padding, cube_size):
    x, y, z = cube_key
    start_coords = np.array([x*cube_size - padding, y*cube_size - padding, z*cube_size - padding]) + cube_size # spelufo offset, 1 indexing
    grid_block_size = cube_size + 2*padding + 1
    return start_coords, grid_block_size

def load_and_process_grid_volume(layers, cubes, cube, args, path_template, axis_swap_trans):
    # construct volume indexing
    cube_ppm = cubes[cube]
    xyz = torch.tensor(np.array([c[2:5] for c in cube_ppm], dtype=np.float32), dtype=torch.float32).cuda()
    normals = torch.tensor(np.array([c[5:] for c in cube_ppm], dtype=np.float32), dtype=torch.float32).cuda()
    # construct all coordinate in positive and negative r
    coords = torch.cat([xyz + r * normals for r in range(-args.r, args.r+1)], dim=0)

    # find min and max values in each dimension
    coords_cpu = coords.cpu().numpy()
    min_coords = np.min(coords_cpu, axis=0).astype(np.int32)
    max_coords = np.max(coords_cpu, axis=0).astype(np.int32)
    start_coords = np.array(min_coords).astype(np.int32)
    axis_swap = [1, 0, 2]
    start_coords = start_coords[axis_swap] + args.cube_size
    grid_block_size = np.array(max_coords - min_coords + 1).astype(np.int32)[axis_swap]

    grid_volume = load_grid(path_template, tuple(start_coords), grid_block_size, args.cube_size, uint8=False).astype(np.float32)
    grid_volume = np.transpose(grid_volume.copy(), axes=axis_swap_trans)
    grid_volume = torch.from_numpy(grid_volume).cuda()
    
    # recalculate coords to zero on grid_volume
    coords = coords - torch.tensor(min_coords, dtype=torch.float32, device="cuda")
    
    # extract from grid volume
    samples = extract_from_image_3d(grid_volume, coords).cpu()
    del grid_volume
    del coords

    # construct layers coords
    xy = torch.tensor(np.array([c[:2][::-1] for c in cube_ppm]), dtype=torch.int32).cpu()  # x, y coordinates
    # construct z coordinates for each layer
    z_layers = torch.arange(0, 2*args.r+1, dtype=torch.int32).repeat(len(cube_ppm), 1).T.contiguous().view(-1).cpu()
    # repeat xy coordinates for each layer
    xy_repeated = xy.repeat(2*args.r+1, 1)
    # combine xy and z coordinates
    xyz_layers = torch.cat([z_layers[:, None], xy_repeated], dim=1).cpu()  # z, x, y order
    
    return samples, xyz_layers

def main(args):
    working_path = os.path.dirname(args.ppm_path)
    path_template = working_path + "/" + args.grid_volume_path + "/cell_yxz_{:03}_{:03}_{:03}.tif"

    # load ppm cubes
    cubes, im_shape = load_ppm_cubes(args.ppm_path, cube_size=args.rendering_size)
    print(f"Loaded {len(cubes)} cubes from {args.ppm_path}")

    # pytorch array uint16 on cpu of size 2*r, im_shape
    layers = torch.zeros((2*args.r + 1, im_shape[1], im_shape[0]), dtype=torch.float32, device='cpu')
    layers_path = working_path + "/layers/"

    print(f"All parameters: {args}, im_shape: {im_shape}, layers_path: {layers_path}, path_template: {path_template}")
    axis_swap_trans = [2, 1, 0]
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        # Submit all tasks and store the future objects
        futures = {executor.submit(load_and_process_grid_volume, layers, cubes, cube, args, path_template, axis_swap_trans): cube for cube in cubes.keys()}

        # Initialize tqdm with the total number of tasks
        with tqdm(total=len(futures), desc="Processing Cubes") as progress:
            for future in as_completed(futures):
                # Update the progress bar each time a future is completed
                progress.update(1)

                # Get the result of the completed future and do something with it
                result = future.result()
                samples, xyz_layers = result

                # insert into layers
                insert_into_image_3d(samples, xyz_layers, layers) 

    # save layers
    for i in range(layers.shape[0]):
        nr_zeros = len(str(2*args.r))
        layer = layers[i].cpu().numpy().astype(np.uint16)
        # save layer with leading 0's for 2*r layers
        layer_nr = str(i).zfill(nr_zeros)
        layer_path = layers_path + f"{layer_nr}.tif"
        tifffile.imwrite(layer_path, layer)

if __name__ == '__main__':
    # parse ppm path, grid volume path, r=32, cube_size=500 default, all cores
    parser = argparse.ArgumentParser()
    parser.add_argument('ppm_path', type=str)
    parser.add_argument('grid_volume_path', type=str)
    parser.add_argument('--r', type=int, default=32)
    parser.add_argument('--cube_size', type=int, default=500)
    parser.add_argument('--rendering_size', type=int, default=400)
    parser.add_argument('--max_workers', type=int, default=multiprocessing.cpu_count()//2)
    args = parser.parse_args()

    main(args)