from base import BaseModel
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from model import *
from model.graph_net import GraphNet
from model.binary_pair_net import BinaryPairNet
#from model.roi_align.roi_align import RoIAlign
from model.roi_align import ROIAlign
from skimage import draw
from model.net_builder import make_layers
from utils.yolo_tools import non_max_sup_iou, non_max_sup_dist
import math
import random
import json

import timeit
import cv2

MAX_CANDIDATES=400 #450
#max seen 428, so why'd it crash on 375?

def primeFactors(n): 
    ret = [1]
    # Print the number of two's that divide n 
    while n % 2 == 0: 
        if len(ret)==0:
            ret.append(2)
        n = n / 2
          
    # n must be odd at this point 
    # so a skip of 2 ( i = i + 2) can be used 
    for i in range(3,int(math.sqrt(n))+1,2): 
          
        # while i divides n , print i ad divide n 
        while n % i== 0: 
            ret.append(i) 
            n = n / i 
              
    # Condition if n is a prime 
    # number greater than 2 
    if n > 2: 
        ret.append(n)
    return ret

class PairingGraph(BaseModel):
    def __init__(self, config):
        super(PairingGraph, self).__init__(config)

        if 'detector_checkpoint' in config:
            checkpoint = torch.load(config['detector_checkpoint'])
            detector_config = json.load(open(config['detector_config']))['model'] if 'detector_config' in config else checkpoint['config']['model']
            if 'state_dict' in checkpoint:
                self.detector = eval(checkpoint['config']['arch'])(detector_config)
                self.detector.load_state_dict(checkpoint['state_dict'])
            else:
                self.detector = checkpoint['model']
        else:
            detector_config = config['detector_config']
            self.detector = eval(detector_config['arch'])(detector_config)
        useBeginningOfLast = config['use_beg_det_feats'] if 'use_beg_det_feats' in config else False
        useFeatsLayer = config['use_detect_layer_feats'] if 'use_detect_layer_feats' in config else -1
        self.detector.setForGraphPairing(useBeginningOfLast,useFeatsLayer)
        if (config['start_frozen'] if 'start_frozen' in config else False):
            for param in self.detector.parameters(): 
                param.will_use_grad=param.requires_grad 
                param.requires_grad=False 
            self.detector_frozen=True
        else:
            self.detector_frozen=False


        self.numBBTypes = self.detector.numBBTypes
        self.rotation = self.detector.rotation
        self.scale = self.detector.scale
        self.anchors = self.detector.anchors
        self.confThresh = config['conf_thresh'] if 'conf_thresh' in config else 0.5

        graph_in_channels = config['graph_config']['in_channels'] if 'in_channels' in config['graph_config'] else 1
        self.useBBVisualFeats=True
        if config['graph_config']['arch']=='BinaryPairNet':
            self.useBBVisualFeats=False
        self.includeRelRelEdges= config['use_rel_rel_edges'] if 'use_rel_rel_edges' in config else True
        #rel_channels = config['graph_config']['rel_channels']
        self.pool_h = config['featurizer_start_h']
        self.pool_w = config['featurizer_start_w']


        assert('use_rel_shape_feats' not in config)
        self.useShapeFeats= config['use_shape_feats'] if 'use_shape_feats' in config else False
        #HACK, fixed values
        self.normalizeHorz=400
        self.normalizeVert=50

        assert(self.detector.scale[0]==self.detector.scale[1])
        detect_scale = self.detector.scale[0]
        self.roi_align = ROIAlign(self.pool_h,self.pool_w,1.0/detect_scale)
        self.avg_box = ROIAlign(2,3,1.0/detect_scale)

        feat_norm = detector_config['norm_type'] if 'norm_type' in detector_config else None
        featurizer_conv = config['featurizer_conv'] if 'featurizer_conv' in config else [512,'M',512]
        featurizer_conv = [self.detector.last_channels+2] + featurizer_conv #+2 for bb masks
        scaleX=1
        scaleY=1
        for a in featurizer_conv:
            if a=='M' or (type(a) is str and a[0]=='D'):
                scaleX*=2
                scaleY*=2
            elif type(a) is str and a[0]=='U':
                scaleX/=2
                scaleY/=2
            elif type(a) is str and a[0:4]=='long': #long pool
                scaleX*=3
                scaleY*=2
        #self.scale=(scaleX,scaleY) this holds scale for detector
        fsizeX = self.pool_w//scaleX
        fsizeY = self.pool_h//scaleY
        layers, last_ch_relC = make_layers(featurizer_conv,norm=feat_norm) #we just don't dropout here
        layers.append( nn.AvgPool2d((fsizeY,fsizeX)) )
        self.relFeaturizerConv = nn.Sequential(*layers)

        if self.useShapeFeats:
           added_feats=8+2*self.numBBTypes #we'll append some extra feats
           added_featsBB=3+self.numBBTypes
        else:
           added_feats=0
           added_featsBB=0
        featurizer_fc = config['featurizer_fc'] if 'featurizer_fc' in config else []
        if config['graph_config']['arch']=='BinaryPairNet':
            feat_norm=None
            featurizer_fc = [last_ch_relC+added_feats] + featurizer_fc + ['FCnR{}'.format(graph_in_channels)]
        else:
            featurizer_fc = [last_ch_relC+added_feats] + featurizer_fc + ['FCnR{}'.format(graph_in_channels)]
        layers, last_ch_rel = make_layers(featurizer_fc,norm=feat_norm) #we just don't dropout here
        self.relFeaturizerFC = nn.Sequential(*layers)

        if self.useBBVisualFeats:
            #TODO un-hardcode
            #self.bbFeaturizerConv = nn.MaxPool2d((2,3))
            #self.bbFeaturizerConv = nn.Sequential( nn.Conv2d(self.detector.last_channels,self.detector.last_channels,kernel_size=(2,3)), nn.ReLU(inplace=True) )
            featurizer = config['node_featurizer'] if 'node_featurizer' in config else []
            if len(featurizer)>0:
                convOut=featurizer[0]
            else:
                convOut=graph_in_channels-added_featsBB
            factors=primeFactors(convOut)
            bestDist=9999
            for f in factors:
                if abs(f-8)<=bestDist: #favor larger
                    bestDist=abs(f-8)
                    bestGroup=f
            self.bbFeaturizerConv = nn.Sequential( 
                    nn.Conv2d(self.detector.last_channels,convOut,kernel_size=(2,3)),
                    nn.GroupNorm(bestGroup,convOut)
                    )
            assert(len(featurizer)==0)
            #if len(featurizer)>0:
            #    featurizer = featurizer + ['FCnR{}'.format(graph_in_channels)]
            #    layers, last_ch_node = make_layers(featurizer,norm=feat_norm)
            #    self.bbFeaturizerFC = nn.Sequential(*layers)
            #else:
            #    self.bbFeaturizerFC = None


        #self.pairer = GraphNet(config['graph_config'])
        self.pairer = eval(config['graph_config']['arch'])(config['graph_config'])


        if 'DEBUG' in config:
            self.detector.setDEBUG()
            self.setDEBUG()
            self.debug=True
        else:
            self.debug=False

 
    def unfreeze(self): 
        if self.detector_frozen:
            for param in self.detector.parameters(): 
                param.requires_grad=param.will_use_grad 
            self.detector_frozen=False
            print('Unfroze detector')
        

    def forward(self, image, gtBBs=None, useGTBBs=False, otherThresh=None, otherThreshIntur=None, hard_detect_limit=300):
        ##tic=timeit.default_timer()
        bbPredictions, offsetPredictions, _,_,_,_ = self.detector(image)
        _=None
        final_features=self.detector.final_features
        self.detector.final_features=None
        ##print('detector: {}'.format(timeit.default_timer()-tic))

        if final_features is None:
            print('ERROR:no final features!')
            import pdb;pdb.set_trace()

        
        ##tic=timeit.default_timer()
        maxConf = bbPredictions[:,:,0].max().item()
        if otherThreshIntur is None:
            confThreshMul = self.confThresh
        else:
            confThreshMul = self.confThresh*(1-otherThreshIntur) + otherThresh*otherThreshIntur
        threshConf = max(maxConf*confThreshMul,0.5)
        if self.rotation:
            bbPredictions = non_max_sup_dist(bbPredictions.cpu(),threshConf,2.5,hard_detect_limit)
        else:
            bbPredictions = non_max_sup_iou(bbPredictions.cpu(),threshConf,0.4,hard_detect_limit)
        #I'm assuming batch size of one
        assert(len(bbPredictions)==1)
        bbPredictions=bbPredictions[0]
        ##print('process boxes: {}'.format(timeit.default_timer()-tic))
        #bbPredictions should be switched for GT for training? Then we can easily use BCE loss. 
        #Otherwise we have to to alignment first
        if not useGTBBs:
            if bbPredictions.size(0)==0:
                return bbPredictions, offsetPredictions, None, None
            useBBs = bbPredictions[:,1:] #remove confidence score
        else:
            if gtBBs is None:
                return bbPredictions, offsetPredictions, None, None
            useBBs = gtBBs[0,:,0:5]
            if self.useShapeFeats:
                classes = gtBBs[0,:,13:]
                #pos = random.uniform(0.51,0.99)
                #neg = random.uniform(0.01,0.49)
                #classes = torch.where(classes==0,torch.tensor(neg).to(classes.device),torch.tensor(pos).to(classes.device))
                pos = torch.rand_like(classes)/2 +0.5
                neg = torch.rand_like(classes)/2
                classes = torch.where(classes==0,neg,pos)
                useBBs = torch.cat((useBBs,classes),dim=1)
        if useBBs.size(0)>1:
            #bb_features, adjacencyMatrix, rel_features = self.createGraph(useBBs,final_features)
            bbAndRel_features, adjacencyMatrix, numBBs, numRel, relIndexes = self.createGraph(useBBs,final_features)
            if bbAndRel_features is None:
                return bbPredictions, offsetPredictions, None, None

            ##tic=timeit.default_timer()
            #nodeOuts, relOuts = self.pairer(bb_features, adjacencyMatrix, rel_features)
            bbOuts, relOuts = self.pairer(bbAndRel_features, adjacencyMatrix, numBBs)
            #bbOuts = graphOut[:numBBs]
            #relOuts = graphOut[numBBs:]
            ##print('pairer: {}'.format(timeit.default_timer()-tic))

            #adjacencyMatrix = torch.zeros((bbPredictions.size(1),bbPredictions.size(1)))
            #for rel in relOuts:
            #    i,j,a=graphToDetectionsMap(

            return bbPredictions, offsetPredictions, relOuts, relIndexes
        else:
            return bbPredictions, offsetPredictions, None, None

    def createGraph(self,bbs,features,debug_image=None):
        ##tic=timeit.default_timer()
        candidates = self.selectCandidateEdges(bbs)
        ##print('  candidate: {}'.format(timeit.default_timer()-tic))
        if len(candidates)==0:
            return None,None,None,None,None
        ##tic=timeit.default_timer()

        #stackedEdgeFeatWindows = torch.FloatTensor((len(candidates),features.size(1)+2,self.relWindowSize,self.relWindowSize)).to(features.device())

        #get corners from bb predictions
        x = bbs[:,0]
        y = bbs[:,1]
        r = bbs[:,2]
        h = bbs[:,3]
        w = bbs[:,4]
        cos_r = torch.cos(r)
        sin_r = torch.sin(r)
        tlX = -w*cos_r + -h*sin_r +x
        tlY =  w*sin_r + -h*cos_r +y
        trX =  w*cos_r + -h*sin_r +x
        trY = -w*sin_r + -h*cos_r +y
        brX =  w*cos_r + h*sin_r +x
        brY = -w*sin_r + h*cos_r +y
        blX = -w*cos_r + h*sin_r +x
        blY =  w*sin_r + h*cos_r +y

        tlX = tlX.cpu()
        tlY = tlY.cpu()
        trX = trX.cpu()
        trY = trY.cpu()
        blX = blX.cpu()
        blY = blY.cpu()
        brX = brX.cpu()
        brY = brY.cpu()

        #get axis aligned rectangle from corners
        rois = torch.zeros((len(candidates),5)) #(batchIndex,x1,y1,x2,y2) as expected by ROI Align
        i=0
        for (index1, index2) in candidates:
            maxX = max(tlX[index1],tlX[index2],trX[index1],trX[index2],blX[index1],blX[index2],brX[index1],brX[index2])
            minX = min(tlX[index1],tlX[index2],trX[index1],trX[index2],blX[index1],blX[index2],brX[index1],brX[index2])
            maxY = max(tlY[index1],tlY[index2],trY[index1],trY[index2],blY[index1],blY[index2],brY[index1],brY[index2])
            minY = min(tlY[index1],tlY[index2],trY[index1],trY[index2],blY[index1],blY[index2],brY[index1],brY[index2])
            rois[i,1]=minX
            rois[i,2]=minY
            rois[i,3]=maxX
            rois[i,4]=maxY
            i+=1

            ###DEBUG
            if debug_image is not None and i<10:
                assert(self.rotation==False)
                #print('crop {}: ({},{}), ({},{})'.format(i,minX.item(),maxX.item(),minY.item(),maxY.item()))
                #print(bbs[index1])
                #print(bbs[index2])
                crop = debug_image[0,:,int(minY.item()):int(maxY.item()),int(minX.item()):int(maxX.item())+1].cpu()
                crop = (2-crop)/2
                if crop.size(0)==1:
                    crop = crop.expand(3,crop.size(1),crop.size(2))
                crop[0,int(tlY[index1].item()-minY.item()):int(brY[index1].item()-minY.item())+1,int(tlX[index1].item()-minX.item()):int(brX[index1].item()-minX.item())+1]*=0.5
                crop[1,int(tlY[index2].item()-minY.item()):int(brY[index2].item()-minY.item())+1,int(tlX[index2].item()-minX.item()):int(brX[index2].item()-minX.item())+1]*=0.5
                crop = crop.numpy().transpose([1,2,0])
                cv2.imshow('crop {}'.format(i),crop)
                #import pdb;pdb.set_trace()
            ###
        if debug_image is not None:
            cv2.waitKey()

        #crop from feats, ROI pool
        stackedEdgeFeatWindows = self.roi_align(features,rois.to(features.device))

        #create and add masks
        masks = torch.zeros(stackedEdgeFeatWindows.size(0),2,stackedEdgeFeatWindows.size(2),stackedEdgeFeatWindows.size(3))
        if self.useShapeFeats:
            shapeFeats = torch.FloatTensor(len(candidates),8+2*self.numBBTypes)
        i=0
        for (index1, index2) in candidates:
            #... or make it so index1 is always to top-left one
            if random.random()<0.5 and not self.debug:
                temp=index1
                index1=index2
                index2=temp
            
            #warp to roi space
            feature_w = rois[i,3]-rois[i,1] +1
            feature_h = rois[i,4]-rois[i,2] +1
            w_m = self.pool_w/feature_w
            h_m = self.pool_h/feature_h

            tlX1 = round(((tlX[index1]-rois[i,1])*w_m).item())
            trX1 = round(((trX[index1]-rois[i,1])*w_m).item())
            brX1 = round(((brX[index1]-rois[i,1])*w_m).item())
            blX1 = round(((blX[index1]-rois[i,1])*w_m).item())
            tlY1 = round(((tlY[index1]-rois[i,2])*h_m).item())
            trY1 = round(((trY[index1]-rois[i,2])*h_m).item())
            brY1 = round(((brY[index1]-rois[i,2])*h_m).item())
            blY1 = round(((blY[index1]-rois[i,2])*h_m).item())
            tlX2 = round(((tlX[index2]-rois[i,1])*w_m).item())
            trX2 = round(((trX[index2]-rois[i,1])*w_m).item())
            brX2 = round(((brX[index2]-rois[i,1])*w_m).item())
            blX2 = round(((blX[index2]-rois[i,1])*w_m).item())
            tlY2 = round(((tlY[index2]-rois[i,2])*h_m).item())
            trY2 = round(((trY[index2]-rois[i,2])*h_m).item())
            brY2 = round(((brY[index2]-rois[i,2])*h_m).item())
            blY2 = round(((blY[index2]-rois[i,2])*h_m).item())

            rr, cc = draw.polygon([tlY1,trY1,brY1,blY1],[tlX1,trX1,brX1,blX1], [self.pool_h,self.pool_w])
            masks[i,0,rr,cc]=1
            rr, cc = draw.polygon([tlY2,trY2,brY2,blY2],[tlX2,trX2,brX2,blX2], [self.pool_h,self.pool_w])
            masks[i,1,rr,cc]=1

            if self.useShapeFeats:
                shapeFeats[i,0] = (bbs[index1,0]-bbs[index2,0])/self.normalizeHorz
                shapeFeats[i,1] = (bbs[index1,1]-bbs[index2,1])/self.normalizeVert
                shapeFeats[i,2] = bbs[index1,2]/math.pi
                shapeFeats[i,3] = bbs[index2,2]/math.pi
                shapeFeats[i,4] = bbs[index1,3]/self.normalizeVert
                shapeFeats[i,5] = bbs[index2,3]/self.normalizeVert
                shapeFeats[i,6] = bbs[index1,4]/self.normalizeHorz
                shapeFeats[i,7] = bbs[index2,4]/self.normalizeHorz
                shapeFeats[i,8:8+self.numBBTypes] = torch.sigmoid(bbs[index1,5:])
                shapeFeats[i,8+self.numBBTypes:8+self.numBBTypes+self.numBBTypes] = torch.sigmoid(bbs[index2,5:])

            i+=1

        stackedEdgeFeatWindows = torch.cat((stackedEdgeFeatWindows,masks.to(stackedEdgeFeatWindows.device)),dim=1)
        #import pdb; pdb.set_trace()
        relFeats = self.relFeaturizerConv(stackedEdgeFeatWindows) #preparing for graph feature size
        relFeats = relFeats.view(relFeats.size(0),relFeats.size(1))
        if self.useShapeFeats:
            relFeats = torch.cat((relFeats,shapeFeats.to(relFeats.device)),dim=1)
        relFeats = self.relFeaturizerFC(relFeats)
        #?
        #?crop bbs
        #?run bbs through net
        if self.useBBVisualFeats:
            assert(features.size(0)==1)
            if self.useShapeFeats:
                bb_shapeFeats=torch.FloatTensor(bbs.size(0),3+self.numBBTypes)
            rois = torch.zeros((bbs.size(0),5))
            for i in range(bbs.size(0)):
                minY = round(min(tlY[i].item(),trY[i].item(),blY[i].item(),brY[i].item()))
                maxY = round(max(tlY[i].item(),trY[i].item(),blY[i].item(),brY[i].item()))
                minX = round(min(tlX[i].item(),trX[i].item(),blX[i].item(),brX[i].item()))
                maxX = round(max(tlX[i].item(),trX[i].item(),blX[i].item(),brX[i].item()))
                rois[i,1]=minX
                rois[i,2]=minY
                rois[i,3]=maxX
                rois[i,4]=maxY
                if self.useShapeFeats:
                    bb_shapeFeats[i,0]= (bbs[i,2]+math.pi)/(2*math.pi)
                    bb_shapeFeats[i,1]=bbs[i,3]/self.normalizeVert
                    bb_shapeFeats[i,2]=bbs[i,4]/self.normalizeHorz
                    bb_shapeFeats[i,3:]=torch.sigmoid(bbs[i,5:])
                #bb_features[i]= F.avg_pool2d(features[0,:,minY:maxY+1,minX:maxX+1], (1+maxY-minY,1+maxX-minX)).view(-1)
            bb_features = self.avg_box(features,rois.to(features.device))
            bb_features = self.bbFeaturizerConv(bb_features)
            bb_features = bb_features.view(bb_features.size(0),bb_features.size(1))
            if self.useShapeFeats:
                bb_features = torch.cat( (bb_features,bb_shapeFeats.to(bb_features.device)), dim=1 )
            #bb_features = self.bbFeaturizerFC(bb_features) if uncommented, change rot on bb_shapeFeats
        else:
            bb_features = None
        
        #We're not adding diagonal (self-rels) here!
        #Expecting special handeling during graph conv
        #candidateLocs = torch.LongTensor(candidates).t().to(relFeats.device)
        #ones = torch.ones(len(candidates)).to(relFeats.device)
        #adjacencyMatrix = torch.sparse.FloatTensor(candidateLocs,ones,torch.Size([bbs.size(0),bbs.size(0)]))

        #assert(relFeats.requries_grad)
        #rel_features = torch.sparse.FloatTensor(candidateLocs,relFeats,torch.Size([bbs.size(0),bbs.size(0),relFeats.size(1)]))
        #assert(rel_features.requries_grad)
        relIndexes=candidates
        numBB = bbs.size(0)
        numRel = len(candidates)
        if bb_features is None:
            numBB=0
            bbAndRel_features=relFeats
            adjacencyMatrix = None
        else:
            bbAndRel_features = torch.cat((bb_features,relFeats),dim=0)
            if self.includeRelRelEdges:
                relEdges=set()
                i=0
                for bb1,bb2 in candidates:
                    j=0
                    for bbA,bbB in candidates[i:]:
                        if i!=j and bb1==bbA or bb1==bbB or bb2==bbA or bb2==bbB:
                            relEdges.add( (numBB+i,numBB+j) ) #i<j always
                        j+=1   
                    i+=1
                edges = candidates+list(relEdges)
            else:
                edges = candidates
            #add diagonal (self edges)
            for i in range(bbAndRel_features.size(0)):
                edges.append((i,i))
            edgeLocs = torch.LongTensor(edges).t().to(relFeats.device)
            ones = torch.ones(len(edges)).to(relFeats.device)
            adjacencyMatrix = torch.sparse.FloatTensor(edgeLocs,ones,torch.Size([bbAndRel_features.size(0),bbAndRel_features.size(0)]))

        #rel_features = (candidates,relFeats)
        #adjacencyMatrix = None
        ##print('create graph: {}'.format(timeit.default_timer()-tic))
        #return bb_features, adjacencyMatrix, rel_features
        return bbAndRel_features, adjacencyMatrix, numBB, numRel, relIndexes



    def selectCandidateEdges(self,bbs):
        if bbs.size(0)<2:
            return []
        #return list of index pairs


        sin_r = torch.sin(bbs[:,2])
        cos_r = torch.cos(bbs[:,2])
        lx = bbs[:,0] - cos_r*bbs[:,4] 
        ly = bbs[:,1] + sin_r*bbs[:,3]
        rx = bbs[:,0] + cos_r*bbs[:,4] 
        ry = bbs[:,1] - sin_r*bbs[:,3]
        tx = bbs[:,0] - cos_r*bbs[:,4] 
        ty = bbs[:,1] - sin_r*bbs[:,3]
        bx = bbs[:,0] + cos_r*bbs[:,4] 
        by = bbs[:,1] + sin_r*bbs[:,3]
        brX = bbs[:,4]*cos_r-bbs[:,3]*sin_r + bbs[:,0] 
        brY = bbs[:,4]*sin_r+bbs[:,3]*cos_r + bbs[:,1] 
        blX = -bbs[:,4]*cos_r-bbs[:,3]*sin_r + bbs[:,0]
        blY= -bbs[:,4]*sin_r+bbs[:,3]*cos_r + bbs[:,1] 
        trX = bbs[:,4]*cos_r+bbs[:,3]*sin_r + bbs[:,0] 
        trY = bbs[:,4]*sin_r-bbs[:,3]*cos_r + bbs[:,1] 
        tlX = -bbs[:,4]*cos_r+bbs[:,3]*sin_r + bbs[:,0]
        tlY = -bbs[:,4]*sin_r-bbs[:,3]*cos_r + bbs[:,1] 

        minX = min( torch.min(trX), torch.min(tlX), torch.min(blX), torch.min(brX) )
        minY = min( torch.min(trY), torch.min(tlY), torch.min(blY), torch.min(brY) )
        maxX = max( torch.max(trX), torch.max(tlX), torch.max(blX), torch.max(brX) )
        maxY = max( torch.max(trY), torch.max(tlY), torch.max(blY), torch.max(brY) )

        lx-=minX 
        ly-=minY 
        rx-=minX 
        ry-=minY 
        tx-=minX 
        ty-=minY 
        bx-=minX 
        by-=minY 
        trX-=minX
        trY-=minY
        tlX-=minX
        tlY-=minY
        brX-=minX
        brY-=minY
        blX-=minX
        blY-=minY

        scaleCand = 0.5
        minX*=scaleCand
        minY*=scaleCand
        maxX*=scaleCand
        maxY*=scaleCand
        lx  *=scaleCand
        ly  *=scaleCand
        rx  *=scaleCand
        ry  *=scaleCand
        tx  *=scaleCand
        ty  *=scaleCand
        bx  *=scaleCand
        by  *=scaleCand
        trX *=scaleCand
        trY *=scaleCand
        tlX *=scaleCand
        tlY *=scaleCand
        brX *=scaleCand
        brY *=scaleCand
        blX *=scaleCand
        blY *=scaleCand
        h = bbs[:,3]*scaleCand
        w = bbs[:,4]*scaleCand
        r = bbs[:,2]

        distMul=1.0
        while distMul>0.03:

            boxesDrawn = np.zeros( (math.ceil(maxY-minY),math.ceil(maxX-minX)) ,dtype=int)#torch.IntTensor( (maxY-minY,maxX-minX) ).zero_()
            if boxesDrawn.shape[0]==0 or boxesDrawn.shape[1]==0:
                import pdb;pdb.set_trace()
            numBoxes = bbs.size(0)
            for i in range(numBoxes):
                
                #cv2.line( boxesDrawn, (int(tlX[i]),int(tlY[i])),(int(trX[i]),int(trY[i])),i,1)
                #cv2.line( boxesDrawn, (int(trX[i]),int(trY[i])),(int(brX[i]),int(brY[i])),i,1)
                #cv2.line( boxesDrawn, (int(blX[i]),int(blY[i])),(int(brX[i]),int(brY[i])),i,1)
                #cv2.line( boxesDrawn, (int(blX[i]),int(blY[i])),(int(tlX[i]),int(tlY[i])),i,1)

                rr,cc = draw.polygon_perimeter([int(tlY[i]),int(trY[i]),int(brY[i]),int(blY[i])],[int(tlX[i]),int(trX[i]),int(brX[i]),int(blX[i])],boxesDrawn.shape,True)
                boxesDrawn[rr,cc]=i+1

            #how to walk?
            #walk until number found.
            # if in list, end
            # else add to list, continue
            #list is candidates
            maxDist = 600*scaleCand*distMul
            maxDistY = 200*scaleCand*distMul
            minWidth=30
            minHeight=20
            numFan=5
            
            def pathWalk(myId,startX,startY,angle,distStart=0,splitDist=100):
                hit=set()
                lineId = myId+numBoxes
                if angle<-180:
                    angle+=360
                if angle>180:
                    angle-=360
                if (angle>45 and angle<135) or (angle>-135 and angle<-45):
                    #compute slope based on y stepa
                    yStep=-1
                    #if angle==90 or angle==-90:

                    xStep=1/math.tan(math.pi*angle/180.0)
                else:
                    #compute slope based on x step
                    xStep=1
                    yStep=-math.tan(math.pi*angle/180.0)
                if angle>=135 or angle<-45:
                    xStep*=-1
                    yStep*=-1
                distSoFar=distStart
                prev=0
                numSteps=0
                y=startY
                while distSoFar<maxDist and abs(y-startY)<maxDistY:
                    x=int(round(startX + numSteps*xStep))
                    y=int(round(startY + numSteps*yStep))
                    numSteps+=1
                    if x<0 or y<0 or x>=boxesDrawn.shape[1] or y>=boxesDrawn.shape[0]:
                        break
                    here = boxesDrawn[y,x]
                    #print('{} {} {} : {}'.format(x,y,here,len(hit)))
                    if here>0 and here<=numBoxes and here!=myId:
                        if here in hit and prev!=here:
                            break
                        else:
                            hit.add(here)
                            #print('hit {} at {}, {}  ({})'.format(here,x,y,len(hit)))
                            #elif here == lineId or here == myId:
                            #break
                    else:
                        boxesDrawn[y,x]=lineId
                    prev=here
                    distSoFar= distStart+math.sqrt((x-startX)**2 + (y-startY)**2)

                    #if hitting and maxDist-distSoFar>splitMin and (distSoFar-distStart)>splitDist and len(toSplit)==0:
                    #    #split
                    #    toSplit.append((myId,x,y,angle+45,distSoFar,hit.copy(),splitDist*1.5))
                    #    toSplit.append((myId,x,y,angle-45,distSoFar,hit.copy(),splitDist*1.5))

                return hit

            def fan(boxId,x,y,angle,num,hit):
                deg = 90/(num+1)
                curDeg = angle-45+deg
                for i in range(num):
                    hit.update( pathWalk(boxId,x,y,curDeg) )
                    curDeg+=deg

            def drawIt():
                x = bbs[:,0]*scaleCand - minX
                y = bbs[:,1]*scaleCand - minY
                drawn = np.zeros( (math.ceil(maxY-minY),math.ceil(maxX-minX),3))#torch.IntTensor( (maxY-minY,maxX-minX) ).zero_()
                numBoxes = bbs.size(0)
                for a,b in candidates:
                    cv2.line( drawn, (int(x[a]),int(y[a])),(int(x[b]),int(y[b])),(random.random()*0.5,random.random()*0.5,random.random()*0.5),1)
                for i in range(numBoxes):
                    
                    #cv2.line( boxesDrawn, (int(tlX[i]),int(tlY[i])),(int(trX[i]),int(trY[i])),i,1)
                    #cv2.line( boxesDrawn, (int(trX[i]),int(trY[i])),(int(brX[i]),int(brY[i])),i,1)
                    #cv2.line( boxesDrawn, (int(blX[i]),int(blY[i])),(int(brX[i]),int(brY[i])),i,1)
                    #cv2.line( boxesDrawn, (int(blX[i]),int(blY[i])),(int(tlX[i]),int(tlY[i])),i,1)

                    rr,cc = draw.polygon_perimeter([int(tlY[i]),int(trY[i]),int(brY[i]),int(blY[i])],[int(tlX[i]),int(trX[i]),int(brX[i]),int(blX[i])])
                    drawn[rr,cc]=(random.random()*0.8+.2,random.random()*0.8+.2,random.random()*0.8+.2)
                cv2.imshow('res',drawn)
                #cv2.waitKey()

                rows,cols=boxesDrawn.shape
                colorMap = [(0,0,0)]
                for i in range(numBoxes):
                    colorMap.append((random.random()*0.8+.2,random.random()*0.8+.2,random.random()*0.8+.2))
                for i in range(numBoxes):
                    colorMap.append( (colorMap[i+1][0]/3,colorMap[i+1][1]/3,colorMap[i+1][2]/3) )
                draw2 = np.zeros((rows,cols,3))
                for r in range(rows):
                    for c in range(cols):
                        draw2[r,c] = colorMap[int(round(boxesDrawn[r,c]))]
                        #draw[r,c] = (255,255,255) if boxesDrawn[r,c]>0 else (0,0,0)

                cv2.imshow('d',draw2)
                cv2.waitKey()


            candidates=set()
            for i in range(numBoxes):
                boxId=i+1
                toSplit=[]
                hit = set()

                horzDiv = 1+math.ceil(w[i]/minWidth)
                vertDiv = 1+math.ceil(h[i]/minHeight)

                if horzDiv==1:
                    leftW=0.5
                    rightW=0.5
                    hit.update( pathWalk(boxId, tlX[i].item()*leftW+trX[i].item()*rightW, tlY[i].item()*leftW+trY[i].item()*rightW,r[i].item()+90) )
                    hit.update( pathWalk(boxId, tlX[i].item()*leftW+trX[i].item()*rightW, tlY[i].item()*leftW+trY[i].item()*rightW,r[i].item()-90) )
                else:
                    for j in range(horzDiv):
                        leftW = 1-j/(horzDiv-1)
                        rightW = j/(horzDiv-1)
                        hit.update( pathWalk(boxId, tlX[i].item()*leftW+trX[i].item()*rightW, tlY[i].item()*leftW+trY[i].item()*rightW,r[i].item()+90) )
                        hit.update( pathWalk(boxId, tlX[i].item()*leftW+trX[i].item()*rightW, tlY[i].item()*leftW+trY[i].item()*rightW,r[i].item()-90) )

                if vertDiv==1:
                    topW=0.5
                    botW=0.5
                    hit.update( pathWalk(boxId, tlX[i].item()*topW+blX[i].item()*botW, tlY[i].item()*topW+blY[i].item()*botW,r[i].item()+180) )
                    hit.update( pathWalk(boxId, trX[i].item()*topW+brX[i].item()*botW, trY[i].item()*topW+brY[i].item()*botW,r[i].item()) )
                else:
                    for j in range(vertDiv):
                        topW = 1-j/(vertDiv-1)
                        botW = j/(vertDiv-1)
                        hit.update( pathWalk(boxId, tlX[i].item()*topW+blX[i].item()*botW, tlY[i].item()*topW+blY[i].item()*botW,r[i].item()+180) )
                        hit.update( pathWalk(boxId, trX[i].item()*topW+brX[i].item()*botW, trY[i].item()*topW+brY[i].item()*botW,r[i].item()) )
                fan(boxId,tlX[i].item(),tlY[i].item(),r[i].item()+135,numFan,hit)
                fan(boxId,trX[i].item(),trY[i].item(),r[i].item()+45,numFan,hit)
                fan(boxId,blX[i].item(),blY[i].item(),r[i].item()+225,numFan,hit)
                fan(boxId,brX[i].item(),brY[i].item(),r[i].item()+315,numFan,hit)

                for jId in hit:
                    candidates.add( (min(i,jId-1),max(i,jId-1)) )
            
            #print('candidates:{} ({})'.format(len(candidates),distMul))
            #if len(candidates)>1:
            #    drawIt()
            if len(candidates)<MAX_CANDIDATES:
                return list(candidates)
            else:
                distMul*=0.85
        #This is a problem, we couldn't prune down enough
        print("ERROR: could not prune number of candidates down: {}".format(len(candidates)))
        return candidates[:MAX_CANDIDATES]

    def setDEBUG(self):
        self.debug=True
        def save_layerConv0(module,input,output):
            self.debug_conv0=output.cpu()
        self.relFeaturizerConv[0].register_forward_hook(save_layerConv0)
        def save_layerConv1(module,input,output):
            self.debug_conv1=output.cpu()
        self.relFeaturizerConv[1].register_forward_hook(save_layerConv1)
        #def save_layerFC(module,input,output):
            #    self.debug_fc=output.cpu()
        #self.relFeaturizerConv[0].register_forward_hook(save_layerFC)
