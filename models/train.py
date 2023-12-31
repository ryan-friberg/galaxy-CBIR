import numpy as np
import os
import time
import torch
import torch.nn.functional as F
from torchvision import transforms
from tqdm import tqdm 

'''
Defines the model-agnostic training and testing processes
'''


# torchvision surprisingly does not have a built in noise-adding transform
class AddGaussianNoise(object):
    def __init__(self, mean, std, threshold=0.8):
        self.std = std
        # self.std = np.random.uniform(0, std)
        self.mean = mean
        self.threshold = threshold
        
    def __call__(self, tensor):
        device = tensor.device
        mask = tensor > self.threshold
        noisy_tensor = tensor + (torch.randn(tensor.size()) * self.std + self.mean).to(device)
        noisy_tensor[mask] = tensor[mask]
        return noisy_tensor
    
    def __repr__(self):
        return self.__class__.__name__ + '(mean={0}, std={1})'.format(self.mean, self.std)


def generate_positive_pairs(batch, indices, num_augmentations, print_transforms=False):
    # define a set of transformations that do not significantly alter
    # the visual content of the image (retrains visual features, positive associations)
    positive_transform_options = np.array([
        transforms.RandomHorizontalFlip(1.0),
        transforms.RandomVerticalFlip(1.0),
        transforms.RandomRotation(180),
        transforms.RandomPerspective(distortion_scale=0.1, p=1.0),
        transforms.Compose([transforms.CenterCrop(150),
                            transforms.Pad(37)]),
        AddGaussianNoise(0., 0.18)
    ])
    
    positive_pairs = []
    for idx in indices:
        # randomly sample a set of transforms (to get diverse alterations throughout training)
        random_transforms = transforms.Compose(
            np.random.choice(positive_transform_options, size=num_augmentations, replace=False)
        )

        # for visualizations
        if print_transforms:
            print(random_transforms)

        unaltered_image = batch[idx]
        positive_associated_image = random_transforms(unaltered_image)
        positive_pairs.append((unaltered_image, positive_associated_image))
    
    return positive_pairs


def generate_negative_pairs(batch, labels, indices, num_augmentations, dataset, print_transforms=False):
    # sample a new random image from the dataset and define a set of transformations 
    # that alter the image a little more drastically to build negative associations

    negative_transform_options = np.array([
        transforms.RandomHorizontalFlip(1.0),
        transforms.RandomVerticalFlip(1.0),
        transforms.RandomRotation(180),
        transforms.Compose([transforms.CenterCrop(150),
                            transforms.Pad(37)]),
        transforms.RandomPerspective(distortion_scale=0.1, p=1.0),
        transforms.RandomErasing(p=1.0, scale=(0.02, 0.1), ratio=(0.3, 3.3), value='random'),
        transforms.GaussianBlur(kernel_size=5, sigma=(0.1, 0.4)),
        AddGaussianNoise(0., 0.18)
    ])

    negative_pairs = []
    for idx in indices:
        unaltered_image = batch[idx]
        image_idx = labels[idx]
    
        # ensure no duplicate (even though the odds are super low)
        found = False
        while not found:
            neg_idx = np.random.randint(0, len(dataset))
            if neg_idx != image_idx:
                negative_associated_image = dataset[neg_idx][0]
                found = True

        random_transforms = transforms.Compose(
            np.random.choice(negative_transform_options, size=num_augmentations, replace=False)
        )

        # for visualizations
        if print_transforms:
            print(random_transforms)

        negative_associated_image = random_transforms(negative_associated_image).to(unaltered_image.device)
        negative_pairs.append((unaltered_image, negative_associated_image))
    
    return negative_pairs


def save_best_model(model, optimizer, epoch, best_loss, filename):
    state = {
        'epoch': epoch,
        'state_dict': model.state_dict(),
        'best_loss': best_loss,
        'optimizer': optimizer.state_dict(),
    }
    torch.save(state, filename)


def early_stopping(current_loss, best_loss, threshold=0.01):
    # Stop training if the loss improvement is less than the threshold
    return current_loss < best_loss - threshold


def contrastive_loss(scoring_fn, output1, output2, target_labels, margin=1.0):
    similarity_score = scoring_fn(output1, output2)
    loss = torch.mean((1 - target_labels) * torch.pow(similarity_score, 2) +
                      (target_labels) * torch.pow(torch.clamp(margin - similarity_score, min=0.0), 2))
    return similarity_score, loss


def test(model, test_loader, test_dataset, scoring_fn, device):
    # test should likely run inference with query image, and save an image in a designated
    # directory that has the query image and the top-k similar images

    model.eval()
    total_loss = 0
    loose_acc  = 0 # the accuracy is loose because the definition of visual similarity is loose
    with torch.no_grad():
        for batch, labels in tqdm(test_loader, total=len(test_loader)):
            batch, labels = batch.to(device), labels.to(device)
            # select the first element of the batch to generate the positive pair
            query_pair = generate_positive_pairs(batch, [0], 1)

            # make a pair of the first batch image with each other image, first pair is positive
            test_pairs = query_pair + [(batch[0], img) for img in batch[1:]]
            pairwise_labels = [1] + [0] * (len(test_pairs) - 1)
            
            # shuffle the elements in the batch
            batch_data = list(zip(test_pairs, pairwise_labels))
            np.random.shuffle(batch_data)
            test_pairs, pairwise_labels = zip(*batch_data)
            test_pairs, pairwise_labels = list(test_pairs), torch.Tensor(list(pairwise_labels)).to(device)

            # get the contrastive loss between the elements in the batch (individually to get sim scores)
            closest_idx = -1
            closest_score = 0
            for i, pair in enumerate(test_pairs):
                output1 = model(pair[0].unsqueeze(0))
                output2 = model(pair[1].unsqueeze(0))
                score, loss = contrastive_loss(scoring_fn, output1, output2, pairwise_labels[i])
                total_loss += loss
                if (score > closest_score):
                    closest_score = score
                    closest_idx = i

            # if the closest pair is the positive pair, increase the accuracy
            if (pairwise_labels[closest_idx] == 1):
                loose_acc += 1
    
    return (total_loss / len(test_dataset)), (loose_acc / len(test_loader))


# this function is the loose placeholder logic
def train(model, train_loader, val_loader, train_dataset, val_dataset, optim, scoring_fn, device, start_epoch=0, 
          num_epochs=10, num_augmentations=3, validate_interval=1, best_loss=np.Inf, checkpoint_filename='./model_checkpoints/best_model.pt',
          save_computations=False):

    train_output_file = os.path.splitext(checkpoint_filename)[0]+'_training_output.text'
    val_losses, val_loose_accs = [], []
    model.train()
    for epoch in range(start_epoch, start_epoch + num_epochs):
        for batch, labels in tqdm(train_loader, total=len(train_loader)):            
            batch, labels = batch.to(device), labels.to(device)

            optim.zero_grad()
            
            # determine which batch elements of the batch are going to be neg/pos
            if save_computations:
                # make some images positive pairs and the rest negative
                batch_indices = torch.randperm(batch.shape[0])
                split_index   = int(batch.shape[0] * 0.5)
                pos_indices   = batch_indices[:split_index]
                neg_indices   = batch_indices[split_index:]
            else:
                # make a positive and negative pair for each image
                batch_indices = np.arange(0, batch.shape[0])
                pos_indices   = batch_indices
                neg_indices   = batch_indices

            # for each image in the batch we generate a set of pairs of images
            positive_pairs = generate_positive_pairs(batch, pos_indices, num_augmentations)
            negative_pairs = generate_negative_pairs(batch, labels, neg_indices, num_augmentations, train_dataset)
            pairwise_labels = torch.tensor([1] * len(pos_indices) + [0] * len(neg_indices)).to(device)
            train_pairs = positive_pairs + negative_pairs

            # shuffle the elements in the batch
            batch_data = list(zip(train_pairs, pairwise_labels))
            np.random.shuffle(batch_data)
            train_pairs, pairwise_labels = zip(*batch_data)
            train_pairs, pairwise_labels = list(train_pairs), torch.Tensor(list(pairwise_labels)).to(device)

            output1 = model(torch.cat([pair[0].unsqueeze(0) for pair in train_pairs], dim=0))
            output2 = model(torch.cat([pair[1].unsqueeze(0) for pair in train_pairs], dim=0))

            score, loss = contrastive_loss(scoring_fn, output1, output2, pairwise_labels)
            loss.backward()
            optim.step()

        if ((epoch % validate_interval) == 0):
            val_loss, val_loose_acc = test(model, val_loader, val_dataset, scoring_fn, device)
            print("Epoch %d - Val Loss: %.3f - Val Loose Acc: %.3f" % (epoch, val_loss, val_loose_acc))

            val_losses.append(val_loss)
            val_loose_accs.append(val_loose_acc)

            if (val_loss < best_loss):
                best_loss = val_loss
                print("Saving model!")
                save_best_model(model, optim, epoch, best_loss, checkpoint_filename)
            
            if early_stopping(val_loss, best_loss):
                print(f"Early stopping triggered at epoch {epoch}")
                break
            
            with open(train_output_file, 'w') as f:
                for loss in val_losses:
                    f.write("%.3f " % loss)
                f.write("\n")
                for acc in val_loose_accs:
                    f.write("%.3f " % acc)
    
    return val_losses, val_loose_accs