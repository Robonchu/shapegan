from itertools import count

import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np

import random
import time
import sys
from collections import deque

from model.sdf_net import SDFNet
from model.gan import Discriminator, LATENT_CODE_SIZE
from util import create_text_slice, device, standard_normal_distribution

from dataset import dataset as dataset, VOXEL_SIZE, SDF_CLIPPING
from loss import inception_score
from util import create_text_slice

from tqdm import tqdm

dataset.rescale_sdf = False
dataset.load_voxels(device)

LEARN_RATE = 0.00005
BATCH_SIZE = 8
CRITIC_UPDATES_PER_GENERATOR_UPDATE = 5
CRITIC_WEIGHT_LIMIT = 0.01

generator = SDFNet()
generator.filename = 'hybrid_wgan_generator.to'

critic = Discriminator()
critic.filename = 'hybrid_wgan_critic.to'
critic.use_sigmoid = False

if "continue" in sys.argv:
    generator.load()
    critic.load()

log_file = open("plots/hybrid_wgan_training.csv", "a" if "continue" in sys.argv else "w")

generator_optimizer = optim.Adam(generator.parameters(), lr=LEARN_RATE)

critic_criterion = torch.nn.functional.binary_cross_entropy
critic_optimizer = optim.RMSprop(critic.parameters(), lr=LEARN_RATE)

show_viewer = "nogui" not in sys.argv

if show_viewer:
    from voxel.viewer import VoxelViewer
    viewer = VoxelViewer()



valid_target_default = torch.ones(BATCH_SIZE, requires_grad=False).to(device)
fake_target_default = torch.zeros(BATCH_SIZE, requires_grad=False).to(device)

def create_batches(sample_count, batch_size):
    batch_count = int(sample_count / batch_size)
    indices = list(range(sample_count))
    random.shuffle(indices)
    for i in range(batch_count - 1):
        yield indices[i * batch_size:(i+1)*batch_size]
    yield indices[(batch_count - 1) * batch_size:]

def create_grid_points():
    sample_points = np.meshgrid(
        np.linspace(-1, 1, VOXEL_SIZE),
        np.linspace(-1, 1, VOXEL_SIZE),
        np.linspace(-1, 1, VOXEL_SIZE)
    )
    sample_points = np.stack(sample_points).astype(np.float32)
    sample_points = np.swapaxes(sample_points, 1, 2)
    sample_points = sample_points.reshape(3, -1).transpose()
    sample_points = torch.tensor(sample_points, device=device)
    return sample_points

def sample_latent_codes(current_batch_size):
    latent_codes = standard_normal_distribution.sample(sample_shape=[current_batch_size, LATENT_CODE_SIZE]).to(device)
    latent_codes = latent_codes.repeat((1, 1, grid_points.shape[0])).reshape(-1, LATENT_CODE_SIZE)
    return latent_codes


grid_points = create_grid_points()
history_fake = deque(maxlen=50)
history_real = deque(maxlen=50)

def train():
    for epoch in count():
        batch_index = 0
        epoch_start_time = time.time()
        for batch in tqdm(list(create_batches(dataset.size, BATCH_SIZE))):
            try:
                indices = torch.tensor(batch, device = device)
                current_batch_size = indices.shape[0] # equals BATCH_SIZE for all batches except the last one
                batch_grid_points = grid_points.repeat((current_batch_size, 1))
                
                # train critic
                fake_target = fake_target_default if current_batch_size == BATCH_SIZE else torch.zeros(current_batch_size, requires_grad=False).to(device)
                valid_target = valid_target_default if current_batch_size == BATCH_SIZE else torch.ones(current_batch_size, requires_grad=False).to(device)

                critic_optimizer.zero_grad()                
                latent_codes = sample_latent_codes(current_batch_size)
                fake_sample = generator.forward(batch_grid_points, latent_codes)
                fake_sample = fake_sample.reshape(-1, VOXEL_SIZE, VOXEL_SIZE, VOXEL_SIZE)
                valid_sample = dataset.voxels[indices, :, :, :]

                critic_output_fake = critic.forward(fake_sample)
                critic_output_valid = critic.forward(valid_sample)

                critic_loss = torch.mean(critic_output_fake) - torch.mean(critic_output_valid)
                critic_loss.backward()
                critic_optimizer.step()
                critic.clip_weights(CRITIC_WEIGHT_LIMIT)

                # train generator
                if batch_index % CRITIC_UPDATES_PER_GENERATOR_UPDATE == 0:
                    generator_optimizer.zero_grad()
                    critic.zero_grad()
                    
                    latent_codes = sample_latent_codes(current_batch_size)
                    fake_sample = generator.forward(batch_grid_points, latent_codes)
                    fake_sample = fake_sample.reshape(-1, VOXEL_SIZE, VOXEL_SIZE, VOXEL_SIZE)
                    if batch_index % 20 == 0 and show_viewer:
                        viewer.set_voxels(fake_sample[0, :, :, :].squeeze().detach().cpu().numpy())
                    if batch_index % 20 == 0 and "show_slice" in sys.argv:
                        print(create_text_slice(fake_sample[0, :, :, :] / SDF_CLIPPING))
                    
                    critic_output_fake = critic.forward(fake_sample)
                    fake_loss = torch.mean(-torch.log(critic_output_fake))
                    fake_loss.backward()
                    generator_optimizer.step()
                    
                    history_fake.append(torch.mean(critic_output_fake).item())
                    history_real.append(torch.mean(critic_output_valid).item())

                if "verbose" in sys.argv and batch_index % 20 == 0:
                    print("Epoch " + str(epoch) + ", batch " + str(batch_index) +
                        ": prediction on fake samples: " + '{0:.4f}'.format(history_fake[-1]) +
                        ", prediction on valid samples: " + '{0:.4f}'.format(history_real[-1]))
                
                batch_index += 1
            except KeyboardInterrupt:
                if show_viewer:
                    viewer.stop()
                return
        
        generator.save()
        critic.save()

        generator.save(epoch=epoch)
        critic.save(epoch=epoch)

        if "show_slice" in sys.argv:
            latent_code = sample_latent_codes(1)
            voxels = generator.forward(grid_points, latent_code)
            voxels = voxels.reshape(VOXEL_SIZE, VOXEL_SIZE, VOXEL_SIZE)
            print(create_text_slice(voxels / SDF_CLIPPING))

        score = generator.get_inception_score()
        prediction_fake = np.mean(history_fake)
        prediction_real = np.mean(history_real)
        print('Epoch {:d} ({:.1f}s), inception score: {:.4f}, prediction on fake: {:.4f}, prediction on real: {:.4f}'.format(epoch, time.time() - epoch_start_time, score, prediction_fake, prediction_real))
        log_file.write('{:d} {:.1f} {:.4f} {:.4f} {:.4f}\n'.format(epoch, time.time() - epoch_start_time, score, prediction_fake, prediction_real))
        log_file.flush()


train()
log_file.close()
