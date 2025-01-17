from __future__ import print_function
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from AverageMeter import AverageMeter
from NCECriterion import NCESoftmaxLoss
from other_utils import set_bn_train, moment_update
from utils_mixup_v2 import *
from losses import *
import time
import warnings
import os, sys
#from apex import amp
import faiss
warnings.filterwarnings('ignore')

def train_sel(args, scheduler,model,model_ema,contrast,queue,device, train_loader, train_selected_loader, optimizer, epoch,features, selected_pair_th,selected_examples):
    train_loss_1 = AverageMeter()
    train_loss_2 = AverageMeter()
    train_loss_3 = AverageMeter()      

    # switch to train mode
    model.train()
    set_bn_train(model_ema)
    end = time.time()
    counter = 1

    criterionCE = torch.nn.CrossEntropyLoss(reduction="none")
    criterion = NCESoftmaxLoss(reduction="none").cuda()
    train_selected_loader_iter = iter(train_selected_loader)
    noisy_labels = torch.LongTensor(train_loader.dataset.targets)
    for batch_idx, (img, labels, index) in enumerate(train_loader):

        img1, img2, labels, index = img[0].to(device), img[1].to(device), labels.to(device), index.to(device)

        bsz = img1.shape[0]

        model.zero_grad()

        ##compute uns-cl loss
        _,feat_q = model(img1)

        with torch.no_grad():
            _, feat_k= model_ema(img2)

        out = contrast(feat_q, feat_k, feat_k, update=True)
        uns_loss = criterion(out)          
            
        ##compute sup-cl loss with selected pairs (adapted from MOIT)
        img1, y_a1, y_b1, mix_index1, lam1 = mix_data_lab(img1, labels, args.alpha_m, device)
        img2, y_a2, y_b2, mix_index2, lam2 = mix_data_lab(img2, labels, args.alpha_m, device)


        predsA, embedA = model(img1)
        predsB, embedB = model(img2)
        predsA = F.softmax(predsA,-1)
        predsB = F.softmax(predsB,-1)
        
        with torch.no_grad():
            predsA_ema, embedA_ema = model_ema(img1)
            predsB_ema, embedB_ema = model_ema(img2)
            predsA_ema = F.softmax(predsA_ema,-1)
            predsB_ema = F.softmax(predsB_ema,-1)

        
        if args.sup_queue_use == 1:
            queue.enqueue_dequeue(torch.cat((embedA_ema.detach(), embedB_ema.detach()), dim=0), torch.cat((predsA_ema.detach(), predsB_ema.detach()), dim=0), torch.cat((index.detach().squeeze(), index.detach().squeeze()), dim=0))

        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
            queue_feats, queue_pros, queue_index = queue.get()
                
        else:
            queue_feats, queue_pros, queue_index = torch.Tensor([]), torch.Tensor([]), torch.Tensor([])
        

        maskUnsup_batch, maskUnsup_mem, mask2Unsup_batch, mask2Unsup_mem = unsupervised_masks_estimation(args, queue, mix_index1, mix_index2, epoch, bsz, device)

        embeds_batch = torch.cat([embedA, embedB], dim=0)
        pros_batch = torch.cat([predsA, predsB], dim=0)
        pairwise_comp_batch = torch.matmul(embeds_batch, embeds_batch.t())
        pros_simi_batch = torch.mm(pros_batch,pros_batch.t())

        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
            embeds_mem = torch.cat([embedA, embedB, queue_feats], dim=0)
            pros_mem = torch.cat([predsA, predsB, queue_pros], dim=0)
            pairwise_comp_mem = torch.matmul(embeds_mem[:2 * bsz], embeds_mem[2 * bsz:].t()) ##Compare mini-batch with memory
            pros_simi_mem = torch.mm(pros_mem[:2 * bsz],pros_mem[2 * bsz:].t())


        maskSup_batch, maskSup_mem, mask2Sup_batch, mask2Sup_mem = \
            supervised_masks_estimation(args, index.long(), queue, queue_index.long(), mix_index1, mix_index2, epoch, bsz, device,features, selected_pair_th, noisy_labels,selected_examples)

        logits_mask_batch = (torch.ones_like(maskSup_batch) - torch.eye(2 * bsz).to(device))  ## Negatives mask, i.e. all except self-contrast sample

        loss_sup = Supervised_ContrastiveLearning_loss(args, pairwise_comp_batch, maskSup_batch, mask2Sup_batch, maskUnsup_batch, mask2Unsup_batch, logits_mask_batch, lam1, lam2, bsz, epoch, device,batch_idx)

        ## compute simi_loss
        loss_simi = Simi_loss(args, pros_simi_batch, maskSup_batch, mask2Sup_batch, maskUnsup_batch, mask2Unsup_batch, logits_mask_batch, lam1, lam2, bsz, epoch, device,batch_idx)
        
        ## using queue
        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:

            logits_mask_mem = torch.ones_like(maskSup_mem) ## Negatives mask, i.e. all except self-contrast sample

            if queue.ptr == 0:
                logits_mask_mem[:, -2 * bsz:] = logits_mask_batch
            else:
                logits_mask_mem[:, queue.ptr - (2 * bsz):queue.ptr] = logits_mask_batch

            loss_mem = Supervised_ContrastiveLearning_loss(args, pairwise_comp_mem, maskSup_mem, mask2Sup_mem, maskUnsup_mem, mask2Unsup_mem, logits_mask_mem, lam1, lam2, bsz, epoch, device,batch_idx)

            loss_sup = loss_sup + loss_mem
            
            loss_simi_mem = Simi_loss(args, pros_simi_mem, maskSup_mem, mask2Sup_mem, maskUnsup_mem, mask2Unsup_mem, logits_mask_mem, lam1, lam2, bsz, epoch, device,batch_idx)
            loss_simi = loss_simi + loss_simi_mem
            
            sel_mask=(maskSup_batch[:bsz].sum(1)+maskSup_mem[:bsz].sum(1))<2
        else:
            sel_mask=(maskSup_batch[:bsz].sum(1))<1

        ## compute class loss with selected examples
        try:
            img, labels, _  = next(train_selected_loader_iter)
        except StopIteration:
            train_selected_loader_iter = iter(train_selected_loader)
            img, labels, _ = next(train_selected_loader_iter)
        img1, img2,  labels = img[0].to(device), img[1].to(device), labels.to(device)
        
        img1, y_a1, y_b1, mix_index1, lam1 = mix_data_lab(img1, labels, args.alpha_m, device)
        img2, y_a2, y_b2, mix_index2, lam2 = mix_data_lab(img2, labels, args.alpha_m, device)

        predsA, embedA = model(img1)
        predsB, embedB = model(img2)


        lossClassif = ClassificationLoss(args, predsA, predsB, y_a1, y_b1, y_a2, y_b2, mix_index1,
                                            mix_index2, lam1, lam2, criterionCE, epoch, device)
        
       
        ## compute sel_loss by combining uns-cl loss and  sup-cl loss 
        sel_loss = (sel_mask*uns_loss).mean() + loss_sup
        
        
        loss = sel_loss + args.lambda_c*lossClassif
        if(args.lambda_s>0):
            # with amp.scale_loss(args.lambda_s*loss_simi, optimizer,loss_id=1) as scaled_loss:
            #     scaled_loss.backward(retain_graph=True)
            # nn.utils.clip_grad_norm_(amp.master_params(optimizer), max_norm=0.25, norm_type=2)
            loss_simi = args.lambda_s*loss_simi
            loss_simi.backward()
            
        
        # with amp.scale_loss(loss, optimizer,loss_id=0) as scaled_loss:
        #     scaled_loss.backward()
        loss.backward()
        optimizer.step()
        scheduler.step()

        moment_update(model, model_ema, args.alpha_moving)
      
        train_loss_1.update(sel_loss.item(), img1.size(0))
        train_loss_2.update(loss_simi.item(), img1.size(0))
        train_loss_3.update(lossClassif.item(), img1.size(0))        
          
        if counter % 15 == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}, Learning rate: {:.6f}'.format(
                epoch, counter * len(img1), len(train_loader.dataset),
                       100. * counter / len(train_loader), 0,
                optimizer.param_groups[0]['lr']))
        counter = counter + 1
    print('train_sel_loss',train_loss_1.avg,'train_simi_loss',train_loss_2.avg,'train_class_loss',train_loss_3.avg)
    print('train time', time.time()-end)

def train_uns(args, scheduler,model,model_ema,contrast,queue,device, train_loader, optimizer, epoch):
    train_loss_1 = AverageMeter()   

    # switch to train mode
    model.train()
    set_bn_train(model_ema)
    end = time.time()
    counter = 1

    criterion = NCESoftmaxLoss(reduction="none").cuda()
    for batch_idx, (img, labels, index) in enumerate(train_loader):

        img1, img2, labels, index = img[0].to(device), img[1].to(device), labels.to(device), index.to(device)

        bsz = img1.shape[0]

        model.zero_grad()

        ##compute uns-cl loss
        _,feat_q = model(img1)

        with torch.no_grad():
            _, feat_k= model_ema(img2)

        out = contrast(feat_q, feat_k, feat_k, update=True)
        uns_loss = criterion(out).mean()        
        
        
        ## update sup queue
        img1, y_a1, y_b1, mix_index1, lam1 = mix_data_lab(img1, labels, args.alpha_m, device)
        img2, y_a2, y_b2, mix_index2, lam2 = mix_data_lab(img2, labels, args.alpha_m, device)

        
        with torch.no_grad():
            predsA_ema, embedA_ema = model_ema(img1)
            predsB_ema, embedB_ema = model_ema(img2)
            predsA_ema = F.softmax(predsA_ema,-1)
            predsB_ema = F.softmax(predsB_ema,-1)

        if args.sup_queue_use == 1:
            queue.enqueue_dequeue(torch.cat((embedA_ema.detach(), embedB_ema.detach()), dim=0), torch.cat((predsA_ema.detach(), predsB_ema.detach()), dim=0), torch.cat((index.detach().squeeze(), index.detach().squeeze()), dim=0))
       
        # with amp.scale_loss(uns_loss, optimizer,loss_id=0) as scaled_loss:
        #     scaled_loss.backward()
        uns_loss.backward()
        optimizer.step()
        scheduler.step()

        moment_update(model, model_ema, args.alpha_moving)
      
        train_loss_1.update(uns_loss.item(), img1.size(0))     
          
        if counter % 15 == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}, Learning rate: {:.6f}'.format(
                epoch, counter * len(img1), len(train_loader.dataset),
                       100. * counter / len(train_loader), 0,
                optimizer.param_groups[0]['lr']))
        counter = counter + 1
    print('train_uns_loss',train_loss_1.avg)
    print('train time', time.time()-end)
    
def train_sup(args, scheduler,model,model_ema,contrast,queue,device, train_loader, train_selected_loader, optimizer, epoch):
    train_loss_1 = AverageMeter()
    train_loss_3 = AverageMeter()      

    # switch to train mode
    model.train()
    set_bn_train(model_ema)
    end = time.time()
    counter = 1

    criterionCE = torch.nn.CrossEntropyLoss(reduction="none")
    train_selected_loader_iter = iter(train_selected_loader)
    noisy_labels = torch.LongTensor(train_loader.dataset.targets)
    for batch_idx, (img, labels, index) in enumerate(train_loader):

        img1, img2, labels, index = img[0].to(device), img[1].to(device), labels.to(device), index.to(device)

        bsz = img1.shape[0]

        model.zero_grad()

        ## update uns queue
        _,feat_q = model(img1)

        with torch.no_grad():
            _, feat_k= model_ema(img2)

        contrast(feat_q, feat_k, feat_k, update=True)
        
        ##compute sup-cl loss with noisy pairs (adapted from MOIT)
        img1, y_a1, y_b1, mix_index1, lam1 = mix_data_lab(img1, labels, 0, device)
        img2, y_a2, y_b2, mix_index2, lam2 = mix_data_lab(img2, labels, 0, device)


        predsA, embedA = model(img1)
        predsB, embedB = model(img2)
        predsA = F.softmax(predsA,-1)
        predsB = F.softmax(predsB,-1)
        
        with torch.no_grad():
            predsA_ema, embedA_ema = model_ema(img1)
            predsB_ema, embedB_ema = model_ema(img2)
            predsA_ema = F.softmax(predsA_ema,-1)
            predsB_ema = F.softmax(predsB_ema,-1)

        if args.sup_queue_use == 1:
            queue.enqueue_dequeue(torch.cat((embedA_ema.detach(), embedB_ema.detach()), dim=0), torch.cat((predsA_ema.detach(), predsB_ema.detach()), dim=0), torch.cat((index.detach().squeeze(), index.detach().squeeze()), dim=0))

        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
            queue_feats, queue_pros, queue_index = queue.get()
                
        else:
            queue_feats, queue_pros, queue_index = torch.Tensor([]), torch.Tensor([]), torch.Tensor([])
        

        maskUnsup_batch, maskUnsup_mem, mask2Unsup_batch, mask2Unsup_mem = unsupervised_masks_estimation(args, queue, mix_index1, mix_index2, epoch, bsz, device)

        embeds_batch = torch.cat([embedA, embedB], dim=0)
        pros_batch = torch.cat([predsA, predsB], dim=0)
        pairwise_comp_batch = torch.matmul(embeds_batch, embeds_batch.t())

        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:
            embeds_mem = torch.cat([embedA, embedB, queue_feats], dim=0)
            pairwise_comp_mem = torch.matmul(embeds_mem[:2 * bsz], embeds_mem[2 * bsz:].t()) ##Compare mini-batch with memory

        maskSup_batch, maskSup_mem, mask2Sup_batch, mask2Sup_mem = \
            supervised_masks_estimation(args, index.long(), queue, queue_index.long(), mix_index1, mix_index2, epoch, bsz, device,None, -1, noisy_labels, None)

        logits_mask_batch = (torch.ones_like(maskSup_batch) - torch.eye(2 * bsz).to(device))  ## Negatives mask, i.e. all except self-contrast sample

        loss_sup = Supervised_ContrastiveLearning_loss(args, pairwise_comp_batch, maskSup_batch, mask2Sup_batch, maskUnsup_batch, mask2Unsup_batch, logits_mask_batch, lam1, lam2, bsz, epoch, device,batch_idx)
        
        if args.sup_queue_use == 1 and epoch > args.sup_queue_begin:

            logits_mask_mem = torch.ones_like(maskSup_mem) ## Negatives mask, i.e. all except self-contrast sample

            if queue.ptr == 0:
                logits_mask_mem[:, -2 * bsz:] = logits_mask_batch
            else:
                logits_mask_mem[:, queue.ptr - (2 * bsz):queue.ptr] = logits_mask_batch

            loss_mem = Supervised_ContrastiveLearning_loss(args, pairwise_comp_mem, maskSup_mem, mask2Sup_mem, maskUnsup_mem, mask2Unsup_mem, logits_mask_mem, lam1, lam2, bsz, epoch, device,batch_idx)

            loss_sup = loss_sup + loss_mem 
            
        ## compute class loss with noisy examples
        try:
            img, labels, _  = next(train_selected_loader_iter)
        except StopIteration:
            train_selected_loader_iter = iter(train_selected_loader)
            img, labels, _ = next(train_selected_loader_iter)
        img1, img2,  labels = img[0].to(device), img[1].to(device), labels.to(device)
        
        img1, y_a1, y_b1, mix_index1, lam1 = mix_data_lab(img1, labels, 0, device)
        img2, y_a2, y_b2, mix_index2, lam2 = mix_data_lab(img2, labels, 0, device)


        predsA, embedA = model(img1)
        predsB, embedB = model(img2)


        lossClassif = ClassificationLoss(args, predsA, predsB, y_a1, y_b1, y_a2, y_b2, mix_index1,
                                            mix_index2, lam1, lam2, criterionCE, epoch, device)
        
                  
        loss = loss_sup.mean() + args.lambda_c*lossClassif
        
        # with amp.scale_loss(loss, optimizer,loss_id=0) as scaled_loss:
        #     scaled_loss.backward()
        loss.backward()
        optimizer.step()
        scheduler.step()

        moment_update(model, model_ema, args.alpha_moving)
      
        train_loss_1.update(loss_sup.item(), img1.size(0))
        train_loss_3.update(lossClassif.item(), img1.size(0))        
          
        if counter % 15 == 0:
            print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}, Learning rate: {:.6f}'.format(
                epoch, counter * len(img1), len(train_loader.dataset),
                       100. * counter / len(train_loader), 0,
                optimizer.param_groups[0]['lr']))
        counter = counter + 1
    print('train_sup_loss',train_loss_1.avg,'train_class_loss',train_loss_3.avg)
    print('train time', time.time()-end)

def pair_selection(args, net, device, trainloader, testloader, epoch,features):

    net.eval()
    temploader = torch.utils.data.DataLoader(trainloader.dataset, batch_size=args.test_batch_size, shuffle=False, num_workers=8)

    ## Weighted k-nn correction
    features_numpy = features.cpu().numpy()
    index = faiss.IndexFlatIP(features_numpy.shape[1])
    index.add(features_numpy)
    labels = torch.LongTensor(trainloader.dataset.targets)
    soft_labels = torch.zeros(len(labels), args.num_classes).scatter_(1, labels.view(-1,1), 1)
    
    D,I = index.search(features_numpy,args.k_val+1)  
    neighbors = torch.LongTensor(I)
    weights = torch.exp(torch.Tensor(D[:,1:])/args.sup_t)  #weight is calculated by embeddings' similarity
    N = features_numpy.shape[0]
    score = torch.zeros(N,args.num_classes)
    
    for n in range(N):           
        neighbor_labels = soft_labels[neighbors[n,1:]]
        score[n] = (neighbor_labels*weights[n].unsqueeze(-1)).sum(0)  #aggregate labels from neighbors
    pseudo_labels = torch.max(score,-1)[1]
    soft_labels = torch.zeros(len(pseudo_labels), args.num_classes).scatter_(1, pseudo_labels.view(-1,1), 1)
    
    for n in range(N):           
        neighbor_labels = soft_labels[neighbors[n,1:]]
        score[n] = (neighbor_labels*weights[n].unsqueeze(-1)).sum(0)  #aggregate labels from neighbors
    soft_labels = score/score.sum(1).unsqueeze(-1)
    
    #soft_labels = torch.from_numpy(soft_labels)
    prob_temp = soft_labels[torch.arange(0, N), labels]
    prob_temp[prob_temp<=1e-2] = 1e-2
    prob_temp[prob_temp > (1-1e-2)] = 1-1e-2
    discrepancy_measure2 = -torch.log(prob_temp)
    agreement_measure = (torch.max(soft_labels, dim=1)[1]==labels).float().data.cpu()

    ## select examples 
    num_clean_per_class = torch.zeros(args.num_classes)
    for i in range(args.num_classes):
        idx_class = temploader.dataset.targets==i
        idx_class = torch.from_numpy(idx_class.astype("float")) == 1.0
        num_clean_per_class[i] = torch.sum(agreement_measure[idx_class])
        
    if(args.alpha==0.5):
        num_samples2select_class = torch.median(num_clean_per_class)
    elif(args.alpha==1.0):
        num_samples2select_class = torch.max(num_clean_per_class)
    elif(args.alpha==0.0):
        num_samples2select_class = torch.min(num_clean_per_class)
    else:
        num_samples2select_class = torch.quantile(num_clean_per_class,args.alpha)
    agreement_measure = torch.zeros((len(temploader.dataset.targets),))

    for i in range(args.num_classes):
        idx_class = temploader.dataset.targets==i
        samplesPerClass = idx_class.sum()
        idx_class = torch.from_numpy(idx_class.astype("float"))# == 1.0
        idx_class = (idx_class==1.0).nonzero().squeeze()
        discrepancy_class = discrepancy_measure2[idx_class]

        if num_samples2select_class>=samplesPerClass:
            k_corrected = samplesPerClass
        else:
            k_corrected = num_samples2select_class

        top_clean_class_relative_idx = torch.topk(discrepancy_class, k=int(k_corrected), largest=False, sorted=False)[1]

        agreement_measure[idx_class[top_clean_class_relative_idx]] = 1.0
    selected_examples=agreement_measure
    print('selected examples',sum(selected_examples)) 
    
    ## select pairs 
    features=features.cuda()
    for i in range(args.num_classes):
        idx_class = temploader.dataset.targets==i
        idx_class = torch.from_numpy(idx_class.astype("float")) == 1.0
        idx_class = (selected_examples.type(torch.bool) & idx_class).nonzero().squeeze()
        class_features = features[idx_class]
        idxes=torch.randint(0, len(idx_class), [len(idx_class)*500])
        random_class_features= class_features[idxes]
        temp_similarities = torch.sum(class_features.repeat(500,1)*random_class_features,-1)
        if(i==0):
            similarities = temp_similarities.data.cpu().numpy()
        else:
            similarities = np.concatenate([similarities, temp_similarities.data.cpu().numpy()])               
    selected_pair_th=np.quantile(similarities,args.beta)
    print('selected_pair_th',selected_pair_th)
        
    return selected_examples,selected_pair_th