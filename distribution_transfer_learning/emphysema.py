
import sys; sys.path.append("/data/miguel/practicum/DL-Medical-Physics")

from core import *
from data_manipulation import DataBatches, RandomRotation, Flip, RandomCrop, balance_obs, multi_label_2_binary
from utils import save_model, load_model, lr_loss_plot
from architectures import DenseNet121
from train_functions import get_optimizer, FinderPolicy, OptimizerWrapper, validate_binary, lr_finder, TTA_binary
import json

BATCH_SIZE = 16
EPOCHS = 20
TRANSFORMATIONS = [RandomRotation(arc_width=20), Flip(), RandomCrop(r_pix=8)]
DATA = 'Pneumonia'
DISEASE = 'Emphysema'

idx2tgt = ['Atelectasis', 'Cardiomegaly', 'Effusion', 'Infiltration', 'Mass', 'Nodule', 'Pneumonia',
               'Pneumothorax', 'Consolidation', 'Edema', 'Emphysema', 'Fibrosis', 'Pleural_Thickening', 'Hernia']

tgt2idx = {disease: i for i, disease in enumerate(idx2tgt)}

SAMPLE_AMOUNTS = [50,100,200,400,600,800,1000,1200,1400,1600,1800,2000]

BASE_PATH = Path('/data/miguel/practicum/')
PATH = BASE_PATH/'data'
SAVE_DIRECTORY = BASE_PATH/'DL-Medical-Physics/distribution_transfer_learning/models'
SAVE_DATA = BASE_PATH/'DL-Medical-Physics/distribution_transfer_learning/results'
IMG_FOLDER = PATH/'ChestXRay-250'
PRETRAINED = True # Imagenet


def cos_annealing(start_lr, end_lr, n_iterations):
    i = np.arange(n_iterations)
    c_i = 1 + np.cos(i * np.pi / n_iterations)
    return end_lr + (start_lr - end_lr) / 2 * c_i

class TrainingPolicy:
    '''Cretes the lr and momentum policy'''

    def __init__(self, n_epochs, dl, max_lr, pctg=.3, moms=(.95, .85),
                 delta=1e-4, div_factor=25.):
        
        total_iterations = n_epochs * len(dl)

        iter1 = int(total_iterations * pctg)
        iter2 = total_iterations - int(total_iterations * pctg)
        iterations = (iter1, iter2)

        min_start = max_lr / div_factor
        min_end = min_start * delta

        lr_segments = ((min_start, max_lr), (max_lr, min_end))
        mom_segments = (moms, (moms[1], moms[0]))

        self.lr_schedule = self._create_schedule(lr_segments, iterations)
        self.mom_schedule = self._create_schedule(mom_segments, iterations)

        self.idx = -1

    def _create_schedule(self, segments, iterations):
        '''
        Creates a schedule given a function, behaviour and size
        '''
        stages = [cos_annealing(start, end, n) for ((start, end), n) in zip(segments, iterations)]
        return np.concatenate(stages)

    def step(self):
        self.idx += 1
        return self.lr_schedule[self.idx], self.mom_schedule[self.idx]
    
def one_cycle_train(n_epochs, train_dl, valid_dl, model, max_lr=.01, wd=0, alpha=1./ 3,
          save_path=None, unfreeze_during_loop:tuple=None):
    
    if unfreeze_during_loop:
        total_iter = n_epochs*len(train_dl)
        first_unfreeze = int(total_iter*unfreeze_during_loop[0])
        second_unfreeze = int(total_iter*unfreeze_during_loop[1])

    best_loss = np.inf
    cnt = 0
    
    policy = TrainingPolicy(n_epochs=n_epochs, dl=train_dl, max_lr=max_lr)
    optimizer = OptimizerWrapper(model, policy, wd=wd, alpha=alpha)

    for epoch in range(n_epochs):
        model.train()
        agg_div = 0
        agg_loss = 0
        train_dl.set_random_choices()
        for x, y in train_dl:

            if unfreeze_during_loop:
                if cnt == first_unfreeze: model.unfreeze(1)
                if cnt == second_unfreeze: model.unfreeze(0)

            out = model(x).squeeze()
            loss = F.binary_cross_entropy_with_logits(input=out, target=y)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            
            batch = y.shape[0]
            agg_loss += batch*loss.item()
            agg_div += batch
            cnt += 1
        
        val_loss, measure, _ = validate_binary(model, valid_dl)
        print(f'Ep. {epoch+1} - train loss {agg_loss/agg_div:.4f} -  val loss {val_loss:.4f} AUC {measure:.4f}')
        
        if save_path and val_loss < best_loss:
            save_model(model, save_path)
            best_loss = val_loss

def keep_only_weight_distribution(model):
    for k,v in model.state_dict().items():
        if 'weight' in k or 'bias' in k: # All layers
            W = model.state_dict()[k].data
            w_shape = W.shape

            mu = W.mean()
            sigma = W.mean()
            
            W_new = sigma*torch.randn_like(W)+mu

            model.state_dict()[k].data.copy_(W_new)
            
# Training            
train_df = pd.read_csv(PATH/"train_df.csv")
valid_df = pd.read_csv(PATH/"val_df.csv")

train_df = multi_label_2_binary(train_df, tgt2idx[DISEASE])

valid_df = multi_label_2_binary(valid_df, tgt2idx[DISEASE])
valid_df = balance_obs(valid_df, amt=2*len(valid_df[valid_df['Label']==1]))

valid_dl = DataBatches(valid_df,img_folder_path=IMG_FOLDER,transforms=False, 
                       shuffle=False, data=DATA, batch_size=BATCH_SIZE, normalize=PRETRAINED)

train_df = train_df.sample(frac=1)

freeze = True
gradual_unfreezing = True

for N in SAMPLE_AMOUNTS:

    df = balance_obs(train_df, amt=N)

    train_dl = DataBatches(df, img_folder_path=IMG_FOLDER, transforms=TRANSFORMATIONS, 
                           shuffle=True, data=DATA, batch_size=BATCH_SIZE, normalize=PRETRAINED)
    
    # normal ImageNet
    model = DenseNet121(1, pretrained=PRETRAINED, freeze=freeze).cuda()

    save_path = SAVE_DIRECTORY/f"{DISEASE.lower()}-std-imgnet-{N}.pth"
    
    one_cycle_train(EPOCHS, train_dl, valid_dl, model, max_lr=.001, save_path=save_path, unfreeze_during_loop=(.1, .2) if gradual_unfreezing else None, alpha=1)
    
    # distribution ImageNet
    model = DenseNet121(1, pretrained=PRETRAINED, freeze=freeze).cuda()
    
    keep_only_weight_distribution(model)

    save_path = SAVE_DIRECTORY/f"{DISEASE.lower()}-dist-imgnet-{N}.pth"
    
    one_cycle_train(EPOCHS, train_dl, valid_dl, model, max_lr=.001, save_path=save_path, unfreeze_during_loop=(.1, .2) if gradual_unfreezing else None, alpha=1./3)
    
# Evaluation

std_imgnet = {
    'losses':[],
    'aucs':[]
}
dist_imgnet = {
    'losses':[],
    'aucs':[]
}

test_df = pd.read_csv(PATH/"test_df.csv")
test_df = multi_label_2_binary(test_df, tgt2idx[DISEASE])
test_df = balance_obs(test_df, amt=2*len(test_df[test_df['Label']==1]))

test_dl = DataBatches(test_df,img_folder_path=IMG_FOLDER, transforms=TRANSFORMATIONS, 
                      shuffle=False, data=DATA, batch_size=BATCH_SIZE, normalize=PRETRAINED)

for i, N in enumerate(SAMPLE_AMOUNTS):

    # std imgnet
    load_path = SAVE_DIRECTORY/f"{DISEASE.lower()}-std-imgnet-{N}.pth"

    load_model(model, load_path)

    loss, mean_auc, _ = TTA_binary(model, test_dl, ndl=4)

    std_imgnet['losses'].append(loss)
    std_imgnet['aucs'].append(mean_auc)
    
    # dist imgnet
    save_path = SAVE_DIRECTORY/f"{DISEASE.lower()}-dist-imgnet-{N}.pth"

    load_model(model, load_path)

    loss, mean_auc, _ = TTA_binary(model, test_dl, ndl=4)

    dist_imgnet['losses'].append(loss)
    dist_imgnet['aucs'].append(mean_auc)


std_imgnet = json.dumps(std_imgnet)
with open(f'results/{DISEASE.lower()}_std-imgnet.json', 'w') as f:
    f.write(std_imgnet)
    
dist_imgnet = json.dumps(dist_imgnet)
with open(f'results/{DISEASE.lower()}_dist-imgnet.json', 'w') as f:
    f.write(dist_imgnet)
