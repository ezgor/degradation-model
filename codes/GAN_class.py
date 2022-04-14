import torch
import torch.nn as nn
from torch import autograd
from torch.nn import functional as F
from torch.utils.data import DataLoader, TensorDataset
from torch.utils.data.dataset import Dataset, random_split
from torch.autograd import Variable
import torchvision
import torchvision.transforms as transforms
from torchvision import datasets
from torchvision.datasets import FashionMNIST
from torchvision.utils import make_grid
from torchvision.utils import save_image
import torch.optim as optim
from codes import my_utils 
from tqdm import tqdm
import numpy as np
from skimage.metrics import peak_signal_noise_ratio as psnr
from skimage.metrics import structural_similarity as ssim 
import time
import lpips
from PIL import Image, ImageFilter
from math import pi

class Generator(nn.Module):
    def __init__(self, channels):
        super(Generator, self).__init__()
        self.convolutional = nn.Sequential(
            nn.Conv2d(in_channels=channels+1, out_channels=channels * 8, kernel_size=3, stride=1,  padding='same', padding_mode='reflect'),
            nn.LeakyReLU(),
            nn.Conv2d(in_channels=channels * 8, out_channels=channels * 64, kernel_size=3, stride=1,  padding='same', padding_mode='reflect'),
            nn.LeakyReLU(),
            nn.Conv2d(in_channels=channels * 64, out_channels=channels * 64, kernel_size=3, stride=2, padding=1, padding_mode='reflect'),
            nn.LeakyReLU(),
            nn.Conv2d(in_channels=channels * 64, out_channels=channels * 8, kernel_size=3, stride=1,  padding='same', padding_mode='reflect'),
            nn.Sigmoid(),
            nn.Conv2d(in_channels=channels * 8, out_channels=channels, kernel_size=3, stride=1,  padding='same', padding_mode='reflect'),
            nn.Sigmoid()
        )

    def forward(self, input):
        return self.convolutional(input)


class Discriminator(nn.Module):
    def __init__(self, ndf=64, nc=3):
        super(Discriminator, self).__init__()
        self.convolutional = nn.Sequential(
            nn.Conv2d(in_channels=nc, out_channels=ndf, kernel_size=5, stride=2, padding=2, bias=False),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(in_channels=ndf, out_channels=ndf * 2, kernel_size=5, stride=2, padding=2, bias=False),
            nn.BatchNorm2d(ndf * 2),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Conv2d(in_channels=ndf * 2, out_channels=1, kernel_size=5, stride=2, padding=0, bias=False),
            nn.AdaptiveAvgPool2d(1)
        )

        self.fullyconnected = nn.Sequential(
                nn.Linear(1, 16),
                nn.LeakyReLU(),
                nn.Linear(16, 256),
                nn.LeakyReLU(),
                nn.Linear(256, 16),
                nn.LeakyReLU(),
                nn.Linear(16, 1),
                nn.Sigmoid()
            )

    def forward(self, input):
        tmp = self.convolutional(input)
        tmp = self.fullyconnected(tmp)
        return tmp        


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif classname.find('BatchNorm') != -1:
        nn.init.normal_(m.weight.data, 1.0, 0.02)
        nn.init.constant_(m.bias.data, 0)


class DownsampleGAN():
    
    def __init__(self, train_loader=None, validation_loader=None, device="cpu",
                 weights_path='./weights/default', models_path='./saved_models/default',
                 img_size=(192, 192), num_channels=3, batch_size=32,
                 channels=3, lr=0.0003, mse_weight=500, epochs=1, ngpu=1):
        
        torch.cuda.empty_cache()
        self.device = device
        # Create the Generator
        self.generator = Generator(num_channels).to(device)
        if (device.type == 'cuda') and (ngpu > 1):
            self.generator = nn.DataParallel(self.generator, list(range(ngpu)))
        _ = self.generator.apply(weights_init)
        # Create the Discriminator
        self.discriminator = Discriminator(num_channels).to(device)
        if (device.type == 'cuda') and (ngpu > 1):
            self.discriminator = nn.DataParallel(self.discriminator, list(range(ngpu)))
        _ = self.discriminator.apply(weights_init)
        
        self.num_channels = num_channels
        self.criterion = nn.BCELoss()
        self.criterionG = nn.MSELoss()
        
        self.img_size = img_size
        self.channels = channels
        self.lr = lr
        self.mse_weight = mse_weight
        self.epochs =  epochs
        self.batch_size = batch_size
        self.trained_epochs = 0
        
        self.train_loader = train_loader
        self.validation_loader = validation_loader
        
        self.weights_path = weights_path
        self.models_path = models_path
        
        self.train_losses = {'G': [], 'D': []}
        self.validation_losses = {'G': [], 'D': []} 
        
        self.configure_optimizers()
        
    
    def configure_optimizers(self):
        lr = self.lr
        self.optG = optim.Adam(self.generator.parameters(), lr=lr, betas=(0.5, 0.999))
        self.optD = optim.Adam(self.discriminator.parameters(), lr=lr, betas=(0.5, 0.999))


    def training_step(self, train_data):
        LRs, HRs = train_data['LQs'], train_data['GT']
        batch_error_G = 0
        batch_error_D = 0
        
        for LR, HR in zip(LRs, HRs):
            ############################
            # (1) Update D network: maximize log(D(x)) + log(1 - D(G(z)))
            ###########################
            ## Train with all-real batch
            self.discriminator.zero_grad()
            real_img = LR.to(self.device)
            real_D = self.discriminator(real_img).view(-1)
            label = torch.ones(real_D.size(), dtype=torch.float, device = self.device) 
            errD_real = self.criterion(real_D, label)
            errD_real.backward()

            ## Train with all-fake batch
            # Generate fake image batch with G     
            HR = my_utils.add_noise(HR, self.img_size)
            fake = self.generator(HR.to(self.device))
            label.fill_(0)
            fake_D = self.discriminator(fake.detach()).view(-1)
            errD_fake = self.criterion(fake_D, label)  
            errD_fake.backward()
            errD = errD_real + errD_fake
            self.optD.step()

            ############################
            # (2) Update G network: maximize log(D(G(z)))
            ###########################
            self.generator.zero_grad()
            label.fill_(1) 
            fake_D = self.discriminator(fake).view(-1)

            errG_mse = self.criterionG(fake, real_img)
            errG = self.criterion(fake_D, label) + self.mse_weight * errG_mse 
            errG.backward()
            self.optG.step()

            batch_error_G += errG.item()
            batch_error_D += errD.item()
        return batch_error_G, batch_error_D
    

    def on_epoch_end(self):   
        self.save_weights(self.weights_path + "/netG_" + str(self.trained_epochs), self.weights_path + "/netD_" + str(self.trained_epochs))
        my_utils.save_model(self, self.models_path + '/mod_' + str(self.trained_epochs))
        self.trained_epochs += 1
        self.lr *= 0.75
        self.configure_optimizers()
        

    def fit(self):

        for epoch in range(self.epochs):
            self.configure_optimizers()
            for i, train_data in tqdm(enumerate(self.train_loader)):
                batch_error_G, batch_error_D = self.training_step(train_data)
                if i % 100 == 0:
                    print('[%d/%d][%d/%d]\tLoss_D: %.4f\tLoss_G: %.4f'
                          % (epoch+1, self.epochs, i, len(self.train_loader),
                            batch_error_D / self.batch_size, batch_error_G / self.batch_size))
                    self.train_losses['G'].append(batch_error_G / self.batch_size)
                    self.train_losses['D'].append(batch_error_D / self.batch_size)

            self.on_epoch_end()


    def save_weights(self, pathG, pathD):
        torch.save(self.generator.state_dict(), pathG)
        torch.save(self.discriminator.state_dict(), pathD)  


    def load_weights(self, pathG, pathD):
        self.generator = Generator(self.num_channels)
        self.generator.load_state_dict(torch.load(pathG))
        self.generator.eval()
        self.generator.to(self.device)

        self.discriminator = Discriminator(self.num_channels)
        self.discriminator.load_state_dict(torch.load(pathD))
        self.discriminator.eval()
        self.discriminator.to(self.device)


    def empty_val_losses(self):
        self.validation_losses = {
            'G': {'GAN': [], 'PSNR': [], 'SSIM': [], 'LPIPS': []}, 
            'D': [],
            'BC': {'PSNR': -1, 'SSIM': -1, 'LPIPS': -1},
            'GS': {'PSNR': -1, 'SSIM': -1, 'LPIPS': -1},
            'BC6': {'PSNR': -1, 'SSIM': -1, 'LPIPS': -1},
            'time': []} 


    def test(self, epoch, flags={}):
        with torch.no_grad():
            error_G = 0
            error_D = 0
            
            total_time = 0

            total_values = {
                'PSNR': {'G': 0, 'BC': 0, 'BC6': 0, 'GS': 0},
                'SSIM': {'G': 0, 'BC': 0, 'BC6': 0, 'GS': 0},
                'LPIPS': {'G': 0, 'BC': 0, 'BC6': 0, 'GS': 0}
                }

            loss_lpips = lpips.LPIPS(net='alex')
            for data in tqdm(self.validation_loader.dataset):
                torch.cuda.empty_cache()
                img_GT = np.array(data['GT'])
                img_LQ = torch.tensor(np.array(data['LQ']))

                HR = torch.tensor(my_utils.get_in(img_GT)).float()
                HR = HR.reshape(1, HR.size(0), HR.size(1), HR.size(2))

                LR = (my_utils.get_in(img_LQ).float()).clone().detach()
                LR = LR.reshape(1, LR.size(0), LR.size(1), LR.size(2))
                
                self.discriminator.zero_grad()
                real_img = LR.to(self.device)
                real_D = self.discriminator(real_img).view(-1)
                label = torch.ones(real_D.size(), dtype=torch.float, device = self.device) 
                errD_real = self.criterion(real_D, label)


                HR = my_utils.add_noise(HR, [HR.size(2), HR.size(3)])
                t0 = time.time()
                fake = self.generator(HR.to(self.device))
                t1 = time.time()
                total_time += (t1 - t0)

                
                img_gen = my_utils.get_out(fake[0]).detach().cpu().numpy()
                img_gt = img_LQ.detach().cpu().numpy()

                if len(flags) > 0:
                    GT = Image.fromarray((img_GT * 255).astype('uint8'), 'RGB')
                    GT_GS = GT.filter(ImageFilter.GaussianBlur(radius = 2/pi))
                    width, height = GT.size
                    img_BC = np.array(GT.resize((width // 2, height // 2),Image.BICUBIC)) / 255
                    img_GS = np.array(GT_GS.resize((width // 2, height // 2),Image.BICUBIC)) / 255
                    img_BC6 = my_utils.bicubic6x(GT)


                    if 'PSNR' in flags:
                        total_values['PSNR']['G'] += psnr(img_gen, img_gt)
                        
                        if self.validation_losses['BC']['PSNR'] == -1:
                            total_values['PSNR']['BC'] += psnr(img_BC, img_gt)
                        
                        if self.validation_losses['GS']['PSNR'] == -1:    
                            total_values['PSNR']['GS'] += psnr(img_GS, img_gt)

                        if self.validation_losses['BC6']['PSNR'] == -1:    
                            total_values['PSNR']['BC6'] += psnr(img_BC6, img_gt)

                    if 'SSIM' in flags:
                        total_values['SSIM']['G'] += ssim(img_gen, img_gt, multichannel=True)
                        if self.validation_losses['BC']['SSIM'] == -1:
                            total_values['SSIM']['BC'] += ssim(img_BC, img_gt, multichannel=True)

                        if self.validation_losses['GS']['SSIM'] == -1:
                            total_values['SSIM']['GS'] += ssim(img_GS, img_gt, multichannel=True)

                        if self.validation_losses['BC6']['SSIM'] == -1:    
                            total_values['SSIM']['BC6'] += ssim(img_BC6, img_gt, multichannel=True)    
                    
                    if 'LPIPS' in flags:
                        total_values['LPIPS']['G'] += loss_lpips(LR, fake.detach().cpu())
                        
                        if self.validation_losses['BC']['LPIPS'] == -1: 
                            LR_BC = torch.tensor(my_utils.get_in(img_BC)).float()
                            LR_BC = LR_BC.reshape(1, LR_BC.size(0), LR_BC.size(1), LR_BC.size(2))
                            total_values['LPIPS']['BC'] += loss_lpips(LR_BC, fake.detach().cpu())

                        if self.validation_losses['BC6']['LPIPS'] == -1: 
                            LR_BC6 = torch.tensor(my_utils.get_in(img_BC6)).float()
                            LR_BC6 = LR_BC6.reshape(1, LR_BC6.size(0), LR_BC6.size(1), LR_BC6.size(2))
                            total_values['LPIPS']['BC6'] += loss_lpips(LR_BC6, fake.detach().cpu())    
                        
                        if self.validation_losses['GS']['LPIPS'] == -1: 
                            LR_GS = torch.tensor(my_utils.get_in(img_GS)).float()
                            LR_GS = LR_GS.reshape(1, LR_GS.size(0), LR_GS.size(1), LR_GS.size(2))
                            total_values['LPIPS']['GS'] += loss_lpips(LR_GS, fake.detach().cpu())


                label.fill_(0)
                fake_D = self.discriminator(fake.detach()).view(-1)
                errD_fake = self.criterion(fake_D, label) 
                errD = errD_real + errD_fake

                self.generator.zero_grad()
                label.fill_(1) 
                fake_D = self.discriminator(fake).view(-1)

                errG_mse = self.criterionG(fake, real_img)
                errG = self.criterion(fake_D, label) + self.mse_weight * errG_mse 

                error_G += errG.item()
                error_D += errD.item()

            vl_len =  len(self.validation_loader)  
            
            av_values = {x: {y: total_values[x][y] / vl_len for y in total_values[x]} for x in total_values}
            av_error_G = error_G / vl_len
            av_error_D = error_D / vl_len
            av_time = total_time / vl_len

            
            self.validation_losses['G']['GAN'].append(av_error_G) 
            self.validation_losses['D'].append(av_error_D)
            self.validation_losses['time'].append(av_time)

            self.validation_losses['G']['SSIM'].append(av_values['SSIM']['G'])
            self.validation_losses['G']['PSNR'].append(av_values['PSNR']['G'])
            self.validation_losses['G']['LPIPS'].append(av_values['LPIPS']['G'])

            for x in av_values:
                for y in av_values[x]:
                    if y != 'G' and av_values[x][y] != 0:
                        self.validation_losses[y][x] =  av_values[x][y]
 

            print('Test Generator loss after %d epoch = ' % int(epoch + 1), av_error_G)
            print('Test Discriminator loss after %d epoch = ' % int(epoch + 1), av_error_D)   
            print('Test PSNR after %d epoch = ' % int(epoch + 1), av_values['PSNR']['G'])
            print('Test SSIM after %d epoch = ' % int(epoch + 1), av_values['SSIM']['G'])
            print('Test LPIPS after %d epoch = ' % int(epoch + 1), av_values['LPIPS']['G'].item())   
            print('Average time for one 1024x512 px frame processing: ', av_time)  
