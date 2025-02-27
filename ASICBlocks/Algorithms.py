import pandas as pd
import numpy as np
from Utils.encode import encode, decode

encodeV = np.vectorize(encode)
decodeV = np.vectorize(decode)

def makeCHARGEQ(row, thresholds):
    nExp = 4
    nMant = 3
    roundBits = False

    asInt  = True
    
    CALQ = row[[f'CALQ_{i}' for i in range(48)]].values

    nDropBit = row['DropLSB']

    raw_charges     = np.array(CALQ[CALQ>=thresholds]).astype(int)
    if len(raw_charges)>0:
        encoded_charges = encodeV(raw_charges,nDropBit,nExp,nMant,roundBits,asInt=True)
    else:
        encoded_charges = np.zeros(48,dtype=int)
        
    return np.pad(encoded_charges,(0,48-len(encoded_charges)),mode='constant',constant_values=0)


def ThresholdSum(df_CALQ, THRESHV_Registers, DropLSB):
    ADD_MAP_Headers = [f'ADDRMAP_{i}' for i in range(48)]
    CHARGEQ_Headers = [f'CHARGEQ_{i}' for i in range(48)]
    if type(THRESHV_Registers) is pd.DataFrame:
        thresholds = THRESHV_Registers.values
    else:
        thresholds = THRESHV_Registers

    df_Threshold_Sum = (df_CALQ>=thresholds).astype(int)
    df_Threshold_Sum.columns = ADD_MAP_Headers

    qlist = df_CALQ.join(DropLSB).apply(makeCHARGEQ,axis=1, args=(thresholds,))
    df_Threshold_Sum[CHARGEQ_Headers] = pd.DataFrame(qlist.values.tolist(),index=qlist.index,columns=CHARGEQ_Headers)
    df_Threshold_Sum['SUM'] = encodeV((df_CALQ).sum(axis=1),0,5,3,False,True)
    df_Threshold_Sum['SUM_NOT_TRANSMITTED'] = encodeV(((df_CALQ<thresholds)*df_CALQ).sum(axis=1),0,5,3,False,True)

    return df_Threshold_Sum



from .bestchoice import sort, batcher_sort

def BestChoice(df_CALQ, DropLSB):
    df_in = pd.DataFrame(df_CALQ.values>>DropLSB.values,columns=df_CALQ.columns, index=df_CALQ.index)

    df_in[df_in>262143]=262143

    df_sorted, _ = sort(df_in)
    df_sorted_index = pd.DataFrame(df_in.apply(batcher_sort, axis=1))

    df_sorted.columns = ['BC_CHARGE_{}'.format(i) for i in range(0, df_sorted.shape[1])]
    df_sorted_index.columns = ['BC_TC_MAP_{}'.format(i) for i in range(0, df_sorted_index.shape[1])]

    df_sorted[df_sorted_index.columns] = df_sorted_index
    return df_sorted



    
from .supertriggercell import supertriggercell_2x2, supertriggercell_4x4

def SuperTriggerCell(df_CALQ, DropLSB):

    stcData_2x2 = df_CALQ.apply(supertriggercell_2x2,axis=1)
    stcData_4x4 = df_CALQ.apply(supertriggercell_4x4,axis=1)

    cols_XTC4_9 = [f'XTC4_9_SUM_{i}' for i in range(12)]
    cols_XTC4_7 = [f'XTC4_7_SUM_{i}' for i in range(12)]
    cols_MAX4_ADDR = [f'MAX4_ADDR_{i}' for i in range(12)]
    
    cols_XTC16_9 = [f'XTC16_9_SUM_{i}' for i in range(3)]
    cols_MAX16_ADDR = [f'MAX16_ADDR_{i}' for i in range(3)]

    df_SuperTriggerCell = pd.DataFrame(stcData_2x2.tolist(),columns = cols_XTC4_9+cols_MAX4_ADDR, index = df_CALQ.index)

    df_SuperTriggerCell[cols_XTC16_9 + cols_MAX16_ADDR] = pd.DataFrame(stcData_4x4.tolist(),columns = cols_XTC16_9+cols_MAX16_ADDR, index = df_CALQ.index)

    for i,c in enumerate(cols_XTC4_9):
        df_SuperTriggerCell[cols_XTC4_7[i]] = encodeV(df_SuperTriggerCell[c].values>>DropLSB.values.flatten(),0,4,3,asInt=True)
        df_SuperTriggerCell[c] = encodeV(df_SuperTriggerCell[c],0,5,4,asInt=True)

    for c in cols_XTC16_9:
        df_SuperTriggerCell[c] = encodeV(df_SuperTriggerCell[c],0,5,4,asInt=True)

    
    return df_SuperTriggerCell[cols_XTC4_9 + cols_XTC16_9 + cols_XTC4_7 + cols_MAX4_ADDR + cols_MAX16_ADDR]




def Repeater(df_CALQ, DropLSB):

    df_in = pd.DataFrame(df_CALQ.values>>DropLSB.values,columns=df_CALQ.columns, index=df_CALQ.index)

    df_Repeater = df_in.apply(encodeV,args=(0,4,3,False,True))
    
    df_Repeater.columns = [f'RPT_{i}' for i in range(48)]
    
    return df_Repeater
    


def Algorithms(df_CALQ, THRESHV_Registers, DropLSB):
    if not type(DropLSB) is pd.DataFrame:
        if type(DropLSB) is int:
            DropLSB = pd.DataFrame({'DropLSB':DropLSB},index=df_CALQ.index)
        else:
            print(f'DropLSB can only be of type int of dataframe, not {type(DropLSB)}')
            exit()

    DropLSB.loc[DropLSB.DropLSB>4] = 0

    df_Threshold_Sum = ThresholdSum(df_CALQ, THRESHV_Registers, DropLSB)
    
    df_BestChoice = BestChoice(df_CALQ, DropLSB)
    
    df_SuperTriggerCell = SuperTriggerCell(df_CALQ, DropLSB)
    
    df_Repeater = Repeater(df_CALQ, DropLSB)
    
    return df_Threshold_Sum, df_BestChoice, df_SuperTriggerCell, df_Repeater

