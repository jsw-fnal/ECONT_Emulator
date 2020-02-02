import uproot
import optparse
import os

import pandas as pd
import numpy as np
import time
import matplotlib.pyplot as plt

from encode import encode, decode
from bestchoice import batcher_sort,sort,sorter
from linkAllocation import linksPerLayer, tcPerLink

from subprocess import Popen,PIPE
import gc

from format import formatThresholdOutput, formatThresholdTruncatedOutput, splitToWords, formatBestChoiceOutput
encodeList = np.vectorize(encode)


def processTree(_tree,geomDF, subdet,layer,jobNumber=0,nEvents=-1):
    #load dataframe
    print('load dataframe')
    if nEvents==-1:
        nEvents=None
    df = _tree.pandas.df( ['tc_subdet','tc_zside','tc_layer','tc_wafer','tc_cell','tc_uncompressedCharge','tc_compressedCharge','tc_data','tc_mipPt'],entrystop=nEvents)
    df.columns = ['subdet','zside','layer','wafer','triggercell','uncompressedCharge','compressedCharge','data','mipPt']
    df.reset_index('subentry',drop=True,inplace=True)

    #remove unwanted layers
#    df = df[(df.subdet==subdet) & (df.layer==layer) & ( df.wafer==wafer)  ]
    df = df[(df.subdet==subdet) & (df.layer==layer)   ]

    #set index
    df.set_index(['subdet','zside','layer','wafer','triggercell'],append=True,inplace=True)
    df.sort_index(inplace=True)

    #split +/- zside into separate entries
    df.reset_index(inplace=True)
    negZ_eventOffset = 5000
    jobNumber_eventOffset = 10000

#    maxN = df.entry.max()
    df.set_index(['zside'],inplace=True)
    df['entry'] = df['entry'] + jobNumber_eventOffset*jobNumber
    df.loc[-1,['entry']] = df.loc[-1,['entry']] + negZ_eventOffset

    df.reset_index(inplace=True)
    df.set_index(['entry','subdet','zside','layer','wafer','triggercell'],inplace=True)

    df.reset_index('entry',inplace=True)

    
    df['isHDM'] = geomDF['isHDM']
    df['eta']   = geomDF['eta']
    df['threshold_ADC'] = geomDF['threshold_ADC']
    
    ## Conversion factor for transverse charge
    df['corrFactor']    = 1./np.cosh(df.eta)
    ## Conversion factor for transverse charge (with finite precision)
    #df['corrFactor_finite']    = truncateFloatList(1./np.cosh(df.eta),8,4)
    precision  = 2**-11
    df['corrFactor_finite']    = round(1./np.cosh(df.eta) / precision) * precision
    #df['threshold_ADC_int'] = geomDF['threshold_ADC_int']

    df.reset_index(inplace=True)
    df.set_index(['entry','subdet','layer','wafer'],inplace=True)
#     df.set_index(['entry','subdet','zside','layer','wafer','triggercell'],inplace=True)
    
    nExp = 4
    nMant = 3
    nDropHDM = 3
    nDropLDM = 1
    roundBits = True
   
    df['encodedCharge'] = np.where(df.isHDM,
                                   df.uncompressedCharge.apply(encode,args=(nDropHDM,nExp,nMant,roundBits,True)),
                                   df.uncompressedCharge.apply(encode,args=(nDropLDM,nExp,nMant,roundBits,True)))
    df['decodedCharge'] = np.where(df.isHDM,
                                   df.encodedCharge.apply(decode,args=(nDropHDM,nExp,nMant)),
                                   df.encodedCharge.apply(decode,args=(nDropLDM,nExp,nMant)))
    
    
    #df.decodedCharge.fillna(0,inplace=True)
    df['calibCharge'] = (df.decodedCharge * df.corrFactor_finite)#.round().astype(np.int)

    #threshold ADC is threshold in transverse charge
    df['pass_135'] = df.decodedCharge>(df.threshold_ADC/df.corrFactor)


    return df.reset_index()


def getGeomDF():
    geomName = "root://cmseos.fnal.gov//store/user/dnoonan/HGCAL_Concentrator/triggerGeomV9.root"
    geomTree = uproot.open(geomName,xrootdsource=dict(chunkbytes=1024**3, limitbytes=1024**3))["hgcaltriggergeomtester/TreeTriggerCells"]
    
    tcmapCSVname = 'TC_ELINK_MAP.csv'
    df_tcmap = pd.read_csv(tcmapCSVname)
    
    geomDF = geomTree.pandas.df(['subdet','zside','layer','wafer','triggercell','x','y','z','c_n'])
    geomDF['r'] = (geomDF['x']**2 + geomDF['y']**2)**.5
    geomDF['eta'] = np.arcsinh(geomDF.z/geomDF.r)
    geomDF.set_index(['subdet','zside','layer','wafer','triggercell'],inplace=True)
    geomDF['isHDM'] = geomDF.c_n>4
    geomDF.sort_index(inplace=True)
    geomDF.drop(['x','y','c_n'],axis=1,inplace=True)
    
    #### Need to update layer list in geomdf for subdet 4 and 5 to match df
    geomDF.reset_index(inplace=True)
    geomDF.loc[geomDF.subdet==4,'layer'] += 28
    geomDF.loc[geomDF.subdet==5,'layer'] += 28
    geomDF.set_index(['subdet','zside','layer','wafer','triggercell'],inplace=True)
    
    threshold_mipPt = 1.35
    fCtoADC = 100./1024.
    geomDF['threshold_fC'] = threshold_mipPt* 3.43      ## threshold on transverse charge
    geomDF['threshold_ADC'] = np.round(geomDF.threshold_fC/fCtoADC).astype(np.int)
    precision = 2**-11
    geomDF['corrFactor_finite']    = round(1./np.cosh(geomDF.eta) / precision) * precision
    return geomDF

def makeAddMAP(tclist):
    #tclist = group of tc >threshold in each wafer
    addmap=np.zeros(48, dtype=int)    #zeros 
    addmap[np.array(list(tclist))]=1  #Mark 1 if tc is present
    return addmap
# def makeCHARGEQ(qlist):
#     #qlist = group of charges in each wafer
#     charges = np.array(list(qlist))     
#     return np.pad(charges,(0,48-len(charges)),mode='constant',constant_values=0)

def makeCHARGEQ(row, nDropBit=1):
    nExp = 4
    nMant = 3
    roundBits = False
#    nDropBit = dropBits ## TODO: make this configurable
    asInt  = True
    
    raw_charges     = np.array(row.dropna()).astype(int)
    if len(raw_charges)>0:
        encoded_charges = encodeList(raw_charges,nDropBit,nExp,nMant,roundBits,asInt=True)
    else:
        encoded_charges = np.zeros(48,dtype=int)
    return np.pad(encoded_charges,(0,48-len(encoded_charges)),mode='constant',constant_values=0)
    

def makeTCindexCols(group,col,iMOD=-1):
    charges=np.zeros(48, dtype=float)    #zeros
    tclist = np.array(list(group['triggercell']))  #indexes of tc
    qlist  = np.array(list(group[col]))            #cols for each tc
    charges[tclist] = qlist                       #assign charges in positions
    if iMOD==-1:
        return list(charges.round().astype(np.int))
    modsum = 0
    if iMOD==0:   modsum = charges[0:16].sum().round().astype(np.int)
    elif iMOD==1: modsum = charges[16:32].sum().round().astype(np.int)
    elif iMOD==2: modsum = charges[32:48].sum().round().astype(np.int)                                             
    return modsum


### the Threshold algo block from CSV inputs
def writeThresAlgoBlock(d_csv):

    df = pd.read_csv(d_csv['calQ_csv'],index_col='entry')
    df_thres = pd.read_csv(d_csv['thres_csv'],header=None)       ## CSV with 1 entry: decodedCharge thresholds of 48 triggercells
    
    df_passThres = pd.DataFrame(index = df.index,columns = df.columns)

    #threshold calibCharge = threshold 
    #default thresholds to high, set ones present in csv to correct values
    calib_thres = np.ones(48,np.int32)*1e9
    calib_thres[df_thres.loc[0].tolist()] = df_thres.loc[1].tolist()
    #    calib_thres  = np.array(df_thres.loc[0].tolist()) 

    #Filter all 48 columns
    for i in range(0,48):
        df_passThres['CALQ_%i'%i] = df[df['CALQ_%i'%i]> calib_thres[i] ]['CALQ_%i'%i]

    df_out       = pd.DataFrame(index = df.index)
    ADD_headers     = ["ADDRMAP_%s"%i for i in range(0,48)]
    CHARGEQ_headers = ["CHARGEQ_%s"%i for i in range(0,48)]

    df_out['NTCQ'] = df_passThres.count(axis=1)         #df_passThres has NAN for TC below threshold
    df_out['SUM']  = df.sum(axis=1).round().astype(np.int)                     #Sum over all charges regardless of threshold
    df_out['MOD_SUM_0']  = df[['CALQ_%i'%i for i in range(0,16)] ].sum(axis=1).round().astype(np.int)      #Sum over all charges of 0-16 TC regardless of threshold
    df_out['MOD_SUM_1']  = df[['CALQ_%i'%i for i in range(16,32)]].sum(axis=1).round().astype(np.int)      #Sum over all charges of 16-32 TC regardless of threshold
    df_out['MOD_SUM_2']  = df[['CALQ_%i'%i for i in range(32,48)]].sum(axis=1).round().astype(np.int)      #Sum over all charges of 32-48 TC regardless of threshold


    df_registers = pd.read_csv(d_csv['register_csv'])
    isHDM = df_registers.isHDM.loc[0]

    ## boolean list of 48 TC cells (filled 0 in df_passThres first)
    tclist                   = df_passThres.fillna(0).apply(lambda x: np.array(x>0).astype(int),axis=1)

    ## calibCharge list of passing TC cells, padding zeros after list 
    nDrop = 3 if isHDM else 1
    qlist                  = df_passThres.apply(makeCHARGEQ, nDropBit=nDrop,axis=1)
    df_out[CHARGEQ_headers] = pd.DataFrame(qlist.values.tolist(),index=qlist.index,columns=CHARGEQ_headers)
    df_out[ADD_headers]     = pd.DataFrame(tclist.values.tolist(),index=tclist.index,columns=ADD_headers)

    ## output to CSV
    cols = np.array(df_out.columns.tolist())
    WAFER = np.logical_and(~np.in1d(cols,ADD_headers),~np.in1d(cols,CHARGEQ_headers))
    df_out.to_csv(d_csv['thres_charge_csv' ],columns=CHARGEQ_headers,index='entry')
    df_out.to_csv(d_csv['thres_address_csv'],columns=ADD_headers,index='entry')
    df_out.to_csv(d_csv['thres_wafer_csv'  ],columns=WAFER,index='entry')


    return df_out


InputLinkGrouping = [[ 0,  1,  2,  3],
                     [ 4,  5,  6,  7],
                     [ 8,  9, 10, 11],
                     [12, 13, 14, 15],
                     [16, 17, 18, 19],
                     [20, 21, 22, 23],
                     [24, 25, 26, 27],
                     [28, 29, 30, 31],
                     [32, 33, 34, 35],
                     [36, 37, 38, 39],
                     [40, 41, 42, 43],
                     [44, 45, 46, 47]]

def packIntoInputLinks(row):
    ENC_headers = ["ENCODED_%s"%i for i in range(0,48)]
    ENC_values = row[ENC_headers].values
    ENC_BIN = np.array([format(x, '#0%ib'%(9))[2:] for x in ENC_values])
    LINK = np.array([ENC_BIN[lgroup][0]+ENC_BIN[lgroup][1]+ENC_BIN[lgroup][2]+ENC_BIN[lgroup][3] for lgroup in InputLinkGrouping])

    return LINK


def writeInputCSV(odir,df,subdet,layer,waferList,appendFile=False):
    writeMode = 'w'
    header=True
    if appendFile:
        writeMode='a'
        header=False

    EPORTRX_headers = ["ePortRxDataGroup_%s"%i for i in range(0,12)]
    ENCODED_headers = ["ENCODED_%s"%i for i in range(0,48)]
    #output of switch matrix ( i.e. decoded charge)
    SM_headers = ["SM_%s"%i for i in range(0,48)]
    #output of Calibration ( i.e. calib charge)
    CALQ_headers = ["CALQ_%s"%i for i in range(0,48)]

    gb = df.groupby(['wafer','entry'],group_keys=False)
#    gb = df.groupby(['subdet','layer','wafer','entry'],group_keys=False)

    encodedlist   = gb[['triggercell','encodedCharge']].apply(makeTCindexCols,'encodedCharge',-1)
    smlist   = gb[['triggercell','decodedCharge']].apply(makeTCindexCols,'decodedCharge',-1)
    calQlist = gb[['triggercell','calibCharge']].apply(makeTCindexCols,'calibCharge',-1)
    df_out     = pd.DataFrame(index=smlist.index)
    df_out[ENCODED_headers]= pd.DataFrame((encodedlist).values.tolist(),index=encodedlist.index)
    df_out[SM_headers]     = pd.DataFrame((smlist).values.tolist(),index=smlist.index)
    df_out[CALQ_headers]   = pd.DataFrame((calQlist).values.tolist(),index=calQlist.index)
    df_out.fillna(0,inplace=True)

    df_out[EPORTRX_headers] = pd.DataFrame(df_out.apply(packIntoInputLinks,axis=1).tolist(),columns=EPORTRX_headers,index=encodedlist.index)

    for _wafer in waferList:
        if odir=='./':
            waferDir = 'wafer_D%iL%iW%i/'%(subdet,layer,_wafer) 
        else:
            waferDir = '%s/wafer_D%iL%iW%i/'%(odir,subdet,layer,_wafer)
        if not os.path.exists(waferDir):
            os.makedirs(waferDir, exist_ok=True)
        
        # df_out     = pd.DataFrame(index=smlist.loc[subdet,layer,_wafer].index)
        # df_out[SM_headers]     = pd.DataFrame((smlist.loc[subdet,layer,_wafer]).values.tolist(),index=smlist.loc[subdet,layer,_wafer].index)
        # df_out[CALQ_headers]   = pd.DataFrame((calQlist.loc[subdet,layer,_wafer]).values.tolist(),index=calQlist.loc[subdet,layer,_wafer].index)
        # df_out.fillna(0,inplace=True)

        waferInput = pd.DataFrame(index=df.entry.unique(),columns=SM_headers+CALQ_headers+EPORTRX_headers)
        waferInput.index.name='entry'
        waferInput[SM_headers+CALQ_headers+EPORTRX_headers] = df_out.loc[_wafer][SM_headers+CALQ_headers+EPORTRX_headers]
        
        waferInput[EPORTRX_headers] = waferInput[EPORTRX_headers].fillna("0000000000000000000000000000")
        waferInput.fillna(0,inplace=True)

        waferInput = waferInput.astype({x:int for x in SM_headers+CALQ_headers})
        
        waferInput.to_csv("%s/SM_output.csv"%waferDir  ,columns=SM_headers,index='entry', mode=writeMode, header=header)
        waferInput.to_csv("%s/CALQ_output.csv"%waferDir,columns=CALQ_headers,index='entry', mode=writeMode, header=header)
        waferInput.to_csv("%s/EPORTRX_output.csv"%waferDir,columns=EPORTRX_headers,index='entry', mode=writeMode, header=header)
        # df_out.loc[_wafer].to_csv("%s/SM_output.csv"%waferDir  ,columns=SM_headers,index='entry', mode=writeMode, header=header)
        # df_out.loc[_wafer].to_csv("%s/CALQ_output.csv"%waferDir,columns=CALQ_headers,index='entry', mode=writeMode, header=header)
        # df_out.loc[_wafer].to_csv("%s/EPORTRX_output.csv"%waferDir,columns=EPORTRX_headers,index='entry', mode=writeMode, header=header)


def writeRegisters(odir,geomDF,subdet,layer,waferList):
    ## write subsidary files
    df_geom = geomDF.reset_index()
    df_geom = df_geom[(df_geom.subdet==subdet) & (df_geom.layer==layer) & (df_geom.zside==1) ]
    df_geom = df_geom.reset_index(drop=True)
    precision  = 2**-11
    df_geom['corrFactor_finite']    = round(1./np.cosh(df_geom.eta) / precision) * precision
#    print(df_geom.head())
#    df_geom.set_index(['wafer'])
    for wafer in waferList:
        if odir=='./':
            waferDir = 'wafer_D%iL%iW%i/'%(subdet,layer,wafer) 
        else:
            waferDir = '%s/wafer_D%iL%iW%i/'%(odir,subdet,layer,wafer) 

        df_geom[df_geom.wafer==wafer][['triggercell','corrFactor_finite']].transpose().to_csv("%s/calib_D%sL%sW%s.csv"%(waferDir,subdet,layer,wafer),index=False,header=None)
        df_geom[df_geom.wafer==wafer][['triggercell','threshold_ADC']].transpose().to_csv("%s/thres_D%sL%sW%s.csv"%(waferDir,subdet,layer,wafer),index=False,header=None)    
        with open("%s/registers_D%sL%sW%s.csv"%(waferDir,subdet,layer,wafer),'w') as outFile:
            outFile.write('isHDM\n')
            outFile.write(f'{df_geom.loc[wafer].isHDM.any()}\n')

    return

# def writeInputCSV(odir,df,subdet,layer,wafer,appendFile=False):
#     writeMode = 'w'
#     header=True
#     if appendFile:
#         writeMode='a'
#         header=False

#     #output of switch matrix ( i.e. decoded charge)
#     SM_headers = ["SM_%s"%i for i in range(0,48)]
#     #output of Calibration ( i.e. calib charge)
#     CALQ_headers = ["CALQ_%s"%i for i in range(0,48)]

#     gb = df.groupby(['entry','subdet','layer','wafer'],group_keys=False)

#     smlist   = gb[['triggercell','decodedCharge']].apply(makeTCindexCols,'decodedCharge',-1)
#     calQlist = gb[['triggercell','calibCharge']].apply(makeTCindexCols,'calibCharge',-1)

#     df_out     = pd.DataFrame(index=smlist.index)
#     df_out[SM_headers]     = pd.DataFrame((smlist).values.tolist(),index=smlist.index)
#     df_out[CALQ_headers]   = pd.DataFrame((calQlist).values.tolist(),index=calQlist.index)
#     df_out.fillna(0,inplace=True)
#     df_out.to_csv("%s/SM_output.csv"%odir  ,columns=SM_headers,index=False, mode=writeMode, header=header)
#     df_out.to_csv("%s/CALQ_output.csv"%odir,columns=CALQ_headers,index=False, mode=writeMode, header=header)


# def writeRegisters(odir,geomDF,subdet,layer,wafer):
#     ## write subsidary files
#     df_geom = geomDF.reset_index()
#     df_geom = df_geom[(df_geom.subdet==subdet) & (df_geom.layer==layer) & ( df_geom.wafer==wafer) & (df_geom.zside==1) ]
#     df_geom = df_geom.reset_index(drop=True)
#     precision  = 2**-11
#     df_geom['corrFactor_finite']    = round(1./np.cosh(df_geom.eta) / precision) * precision
#     df_geom[['triggercell','corrFactor_finite']].transpose().to_csv("%s/calib_D%sL%sW%s.csv"%(odir,subdet,layer,wafer),index=False,header=None)
#     df_geom[['triggercell','threshold_ADC']].transpose().to_csv("%s/thres_D%sL%sW%s.csv"%(odir,subdet,layer,wafer),index=False,header=None)
#     with open("%s/registers_D%sL%sW%s.csv"%(odir,subdet,layer,wafer),'w') as outFile:
#         outFile.write('isHDM\n')
#         outFile.write(f'{df_geom.isHDM.any()}\n')

#     return

def writeThresholdFormat(d_csv):
    df_wafer  = pd.read_csv(d_csv['wafer_csv'],index_col='entry')
    df_addmap = pd.read_csv(d_csv['add_csv'],index_col='entry')
    df_charge = pd.read_csv(d_csv['charge_csv'],index_col='entry')
    
    df_wafer['CHARGEQ'] = df_charge.apply(list,axis=1)
    df_wafer['ADD_MAP'] = df_addmap.apply(list,axis=1)
   
    debug = False

    if not debug: 
        cols = [f'WORD_{i}' for i in range(25)]
        df_wafer['FRAMEQ'] = df_wafer.apply(formatThresholdOutput,args=(debug),axis=1)
        df_wafer['FRAMEQ_TRUNC'] = df_wafer.apply(formatThresholdTruncatedOutput,axis=1)
        df_wafer[cols] = pd.DataFrame(df_wafer.apply(splitToWords,axis=1).tolist(),columns=cols,index=df_wafer.index)
        df_wafer['WORDCOUNT'] = (df_wafer.FRAMEQ.str.len()/16).astype(int)
    else:
        bit_str = df_wafer.apply(formatThresholdOutput,args=(debug),axis=1)
        cols          = ['header', 'dataType' , 'modSumData' ,'extraBit' ,'nChannelData' , 'AddressMapData' ,'ChargeData']
        df_wafer[cols] = pd.DataFrame(bit_str.values.tolist(), index=bit_str.index)
    
#    df_wafer.to_csv(d_csv['format_csv'],columns=['FRAMEQ','FRAMEQ_TRUNC'],index=False)
    df_wafer.to_csv(d_csv['format_csv'],columns=['FRAMEQ','FRAMEQ_TRUNC','WORDCOUNT']+cols,index='entry')
    return

def writeBestChoice(d_csv):
    df_in = pd.read_csv(d_csv['calQ_csv'],index_col='entry')

    df_sorted, _ = sort(df_in)
    df_sorted_index = pd.DataFrame(df_in.apply(batcher_sort, axis=1))

    df_sorted.columns = ['BC_Charge_{}'.format(i) for i in range(0, df_sorted.shape[1])]
#    df_sorted.index.name = 'entry'
    df_sorted_index.columns = ['BC_Address_{}'.format(i) for i in range(0, df_sorted_index.shape[1])]
 #   df_sorted_index.index.name = 'entry'
    df_sorted.to_csv(d_csv['bc_charge_csv'],index='entry')
    df_sorted_index.to_csv(d_csv['bc_address_csv'],index='entry')

    df_registers = pd.read_csv(d_csv['register_csv'])
    isHDM = df_registers.isHDM.loc[0]

    df_sorted[df_sorted_index.columns] = df_sorted_index
#    print (df_sorted.head())
    df_sorted['FRAMEQ'] = df_sorted.apply(formatBestChoiceOutput, args=(d_csv['nTC'],isHDM), axis=1)
    df_sorted['WORDCOUNT'] = (df_sorted.FRAMEQ.str.len()/16).astype(int)
    cols = [f'WORD_{i}' for i in range(25)]
    df_sorted[cols] = pd.DataFrame(df_sorted.apply(splitToWords,axis=1).tolist(),columns=cols,index=df_sorted.index)

    df_sorted.to_csv(d_csv['bc_format_csv'],columns=['FRAMEQ','WORDCOUNT']+cols,index='entry')

    return



def processNtupleInputs(fName, geomDF, subdet, layer, wafer, odir, nEvents, appendFile=False):

    _tree = uproot.open(fName,xrootdsource=dict(chunkbytes=1024**3, limitbytes=1024**3))['hgcalTriggerNtuplizer/HGCalTriggerNtuple']

    print(f'loaded tree {fName}')
    # print(_tree.numentries)
    # writeMode='a' if appendFile else 'w'
    # print (writeMode)

    numString = ''
    for x in fName.split('.root')[0][::-1]:
        if x.isdigit():
            numString = x+numString
        else:
            break
    jobNumber = 999
    if numString.isdigit():
        jobNumber = int(numString)
        
    print(f'file is job number {jobNumber}')

    print ('process tree')
    Layer_df =   processTree(_tree,geomDF,subdet,layer,jobNumber,nEvents)
    del _tree
    gc.collect()

    waferList = Layer_df.wafer.unique()
    if not wafer==-1:
        waferList = [wafer]
    print ('Writing Inputs')

    writeInputCSV(odir,  Layer_df, subdet,layer,waferList,appendFile)

    if not appendFile:
        print ('Writing Registers')
        writeRegisters( odir,  geomDF, subdet,layer,waferList)
            
    return waferList

def runAlgos(subdet, layer, waferList, odir):
    print ('Starting Algos')
    for _wafer in waferList:
        print (_wafer)
        if odir=='./':
            waferDir = 'wafer_D%iL%iW%i/'%(subdet,layer,_wafer) 
        else:
            waferDir = '%s/wafer_D%iL%iW%i/'%(odir,subdet,layer,_wafer) 
#        print(waferDir)
        threshold_inputcsv ={
            'calQ_csv'         :waferDir+'CALQ_output.csv', #input
            'thres_csv'        :waferDir+'thres_D%iL%iW%i.csv'%(subdet,layer,_wafer), #input threshold
            'thres_charge_csv' :waferDir+'threshold_charge.csv',   #output
            'thres_address_csv':waferDir+'threshold_address.csv',  #output
            'thres_wafer_csv'  :waferDir+'threshold_wafer.csv',  #output
            'register_csv'  :waferDir+'registers_D%iL%iW%i.csv'%(subdet,layer,_wafer),
        }
        df_algo         =   writeThresAlgoBlock(threshold_inputcsv)
        bc_inputcsv ={
            'calQ_csv'      :waferDir+'CALQ_output.csv', #input
            'bc_charge_csv' :waferDir+'bc_charge.csv',   #output
            'bc_address_csv':waferDir+'bc_address.csv',  #output
            'bc_format_csv' :waferDir+'bc_formatblock.csv',  #output
            'register_csv'  :waferDir+'registers_D%iL%iW%i.csv'%(subdet,layer,_wafer),
            'nTC': tcPerLink[linksPerLayer[layer]],
        }
        writeBestChoice(bc_inputcsv)
        format_inputcsv ={
            'wafer_csv' :waferDir+'threshold_wafer.csv',        #input
            'add_csv'   :waferDir+'threshold_address.csv',      #input
            'charge_csv':waferDir+'threshold_charge.csv',       #input
            'format_csv':waferDir+'threshold_formatblock.csv',  #output
            'register_csv'  :waferDir+'registers_D%iL%iW%i.csv'%(subdet,layer,_wafer),
        }
        writeThresholdFormat(format_inputcsv)
    
    

def main(opt,args):
    print ('start')

    if not opt.skipInput:
        print('loading')

        fileNameContent = opt.inputFile
        eosDir = opt.eosDir

        fileList = []
        # get list of files                                                                                                                                     
        cmd = "xrdfs root://cmseos.fnal.gov ls %s"%eosDir

        dirContents,stderr = Popen(cmd,shell=True,stdout=PIPE,stderr=PIPE).communicate()
        dirContentsList = dirContents.decode('ascii').split("\n")
        for fName in dirContentsList:
            if fileNameContent in fName:
                fileList.append("root://cmseos.fnal.gov/%s"%(fName))

        fileList = fileList[:opt.Nfiles]
        
        geomDF = getGeomDF()
        for i,fName in enumerate(fileList):
            waferList = processNtupleInputs(fName, geomDF, opt.subdet, opt.layer, opt.wafer, opt.odir, opt.Nevents, appendFile=i>0)
    else:
        if not opt.wafer==-1:
            waferList = [opt.wafer]
        else:
            df_geom = getGeomDF().reset_index()
            df_geom = df_geom[(df_geom.subdet==opt.subdet) & (df_geom.layer==opt.layer) & (df_geom.zside==1) ]

            waferList = df_geom.wafer.unique()

    runAlgos(opt.subdet, opt.layer, waferList, opt.odir)

if __name__=='__main__':
    parser = optparse.OptionParser()
#    parser.add_option('-i',"--inputFile", type="string", default = 'ntuple.root',dest="inputFile", help="input TSG ntuple")
    parser.add_option('-i',"--inputFile", type="string", default = 'ntuple_hgcalNtuples_ttbar_200PU',dest="inputFile", help="input TPG ntuple name format")
    parser.add_option("--eosDir", type="string", default = '/store/user/dnoonan/HGCAL_Concentrator/L1THGCal_Ntuples/TTbar',dest="eosDir", help="direcotyr on eos to find TPG ntuples")
    parser.add_option('-o',"--odir", type="string", default = './',dest="odir", help="output directory")
    parser.add_option('-w',"--wafer" , type=int, default = -1,dest="wafer" , help="which wafer to write")
    parser.add_option('-l',"--layer" , type=int, default = 5 ,dest="layer" , help="which layer to write")
    parser.add_option('-d',"--subdet", type=int, default = 3 ,dest="subdet", help="which subdet to write")
    parser.add_option('-N', type=int, default = -1 ,dest="Nfiles", help="Limit on number of files to read (-1 is all)")
    parser.add_option('--Nevents', type=int, default = -1 ,dest="Nevents", help="Limit on number of events to read per file (-1 is all)")
    parser.add_option("--skipInput", default = False, action='store_true',dest="skipInput", help="skip the read in step, only run algorithms on csv already there")

    (opt, args) = parser.parse_args()

    main(opt,args)