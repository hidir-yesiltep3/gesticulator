"""
This script does the preprocessing of the dataset specified in --proc_data_dir,
and stores the results in the same folder as .npy files.
It should be used before training, as described in the README.md file.

@authors: Taras Kucherenko, Rajmund Nagy
"""
import os
from os import path

import tqdm
import pandas as pd
import numpy as np

from gesticulator.data_processing.text_features.parse_json_transcript import encode_json_transcript_with_bert, encode_json_transcript_with_bert_DEPRECATED
from gesticulator.data_processing import tools
# Params
from gesticulator.data_processing.data_params import processing_argparser

from transformers import BertTokenizer, BertModel


def _encode_vectors(audio_filename, gesture_filename, text_filename, gest_exist_file_path, embedding_model, mode, args, augment_with_context):
    """
    Extract features from a given pair of audio and motion files.
    To be used by "_save_data_as_sequences" and "_save_dataset" functions.

    Args:
        audio_filename:        file name for an audio file (.wav)
        gesture_filename:      Hdf5 group file ('V1'), ('V2')...
        text_filename:         file name with the text transcript (.json)
        gest_exist_file_path   file path with the gesture existance indicators of type [[seconds, gesture_exist]]
        embedding_model:       the embedding model to encode the text with
        mode:                  dataset type ('train', 'dev' or 'test')
        args:                  see the 'create_dataset' function for details
        augment_with_context:  if True, the data sequences will be augmented with future/past context 
                               intended use: True if the data will be used for training,
                                             False if it will be used for validation/testing

    Returns:
        input_vectors  [N, T, D] : speech features
        text_vectors             : text features
        output_vectors [N, T, D] : motion features
    """
    debug = False

    if mode == 'test':
        seq_length = 0
    elif mode == 'train':
        seq_length = args.seq_len
    elif mode == 'dev':
        seq_length = 5 * args.seq_len
    else:
        print(f"ERROR: Unknown dataset type '{mode}'! Possible values: 'train', 'dev' and 'test'.")
        exit(-1)

    # Step 1: Vectorizing speech, with features of 'n_inputs' dimension, time steps of 0.01s
    # and window length with 0.025s => results in an array of 100 x 'n_inputs'
    
    if args.feature_type == "MFCC":

        input_vectors = tools.calculate_mfcc(audio_filename)

    elif args.feature_type == "Pros":

        input_vectors = tools.extract_prosodic_features(audio_filename)

    elif args.feature_type == "MFCC+Pros":

        mfcc_vectors = tools.calculate_mfcc(audio_filename)

        pros_vectors = tools.extract_prosodic_features(audio_filename)

        mfcc_vectors, pros_vectors = tools.shorten(mfcc_vectors, pros_vectors)

        input_vectors = np.concatenate((mfcc_vectors, pros_vectors), axis=1)

    elif args.feature_type =="Spectro":

        input_vectors = tools.calculate_spectrogram(audio_filename)

    elif args.feature_type == "Spectro+Pros":

        spectr_vectors = tools.calculate_spectrogram(audio_filename)

        pros_vectors = tools.extract_prosodic_features(audio_filename)

        spectr_vectors, pros_vectors = tools.shorten(spectr_vectors, pros_vectors)

        input_vectors = np.concatenate((spectr_vectors, pros_vectors), axis=1)

    # Step 2: Read hdf5 file
    motion_data = np.asarray(gesture_filename['body'])

    # Remove lower body
    output_vectors = motion_data[:, upper_body_idxs]

    # Subsample motion (from 60 fps to 20 fps)
    output_vectors = output_vectors[0::3]

    # Step 3: Obtain text transcription:
    if isinstance(embedding_model, tuple):
        text_encoding = encode_json_transcript_with_bert(
            text_filename, tokenizer = embedding_model[0], bert_model = embedding_model[1])
    else:
        raise Exception('Something is wrong with the BERT embedding model')

    if debug:
        print(input_vectors.shape)
        print(output_vectors.shape)
        print(text_encoding.shape)

    # Step 4: Align vector length
    min_len = min(len(input_vectors), len(output_vectors), 2 * len(text_encoding))

    # make sure the length is even
    if min_len % 2 ==1:
        min_len -= 1
    input_vectors, output_vectors = tools.shorten(input_vectors, output_vectors, min_len)
    text_encoding = text_encoding[:int(min_len/2)]

    # Step 5: Load Gesture Existance Indicator file.
    gest_exist_vectors_with_timestamps = np.load(gest_exist_file_path)
    timestamps = gest_exist_vectors[:, 0]
    labels = gest_exist_vectors[:, 1]
    gest_exist_vectors = []
    # TODO: FPS is strictly determined as 20.0 
    each_frame_time = 1 / 20.0 

    curr_frame_idx = 0
    elapsed_time = 0
    
    # Add zeros before the starting timestamp.
    while elapsed_time < timestamps[0]:
        gest_exist_vectors.append([0])
        elapsed_time += each_frame_time
    

    while curr_frame_idx < len(timestamps):
        frame_length = timestamps[1] - timestamps[0] # 0.2 in our case
        while elapsed_time < timestamps[curr_frame_idx] + frame_length:
            gest_exist_vectors.append([labels[curr_frame_idx]])
            elapsed_time += each_frame_time
        curr_frame_idx += 1

    gest_exist_vectors = np.stack(gest_exist_vectors, axis = 0)
    # Pad the end of the vectors if there are missing frames
    n_missing_frames = len(input_vectors) - len(gest_exist_vectors)
    missing_frames = np.zeros((n_missing_frames, 1))

    gest_exist_vectors = np.concatenate((gest_exist_vectors, missing_frames), axis=0)

    if debug:
        print(input_vectors.shape)
        print(output_vectors.shape)
        print(text_encoding.shape)
        print(gest_exist_vectors.shape)

    if not augment_with_context:
        return input_vectors, text_encoding, gest_exist_vectors, output_vectors

    # create a list of sequences with a fixed past and future context length ( overlap them to use data more efficiently)
    # ToDo: make sure the allignment holds
    start_ind = args.past_context
    seq_step = 10 # overlap of sequences: 0.5s

    # Test if the context length is appropriate
    assert args.past_context % 2 == 0
    assert args.future_context % 2 == 0
    assert seq_step % 2 == 0

    n_reserved_inds = seq_length + args.future_context
    
    stop_ind = input_vectors.shape[0] - n_reserved_inds
    input_vectors_final  = np.array([input_vectors[i - args.past_context : i + n_reserved_inds] 
                                     for i in range(start_ind, stop_ind, seq_step)])

    stop_ind = output_vectors.shape[0] - n_reserved_inds
    output_vectors_final = np.array([output_vectors[i - args.past_context : i + n_reserved_inds]
                                     for i in range(start_ind, stop_ind, seq_step)])
    
    stop_ind = gest_exist_vectors.shape[0] - n_reserved_inds
    gest_exist_vectors_final = np.array([gest_exist_vectors_final[i - args.past_context : i + n_reserved_inds]
                                     for i in range(start_ind, stop_ind, seq_step)])
    # The text was sampled at half the sampling rate compared to audio
    # So the 1 frame of text corresponds to 2 frames of audio
    stop_ind = text_encoding.shape[0] - n_reserved_inds // 2
    text_vectors_final   = np.array([text_encoding[i - args.past_context // 2 : i + n_reserved_inds // 2]
                                     for i in range(start_ind // 2, stop_ind, seq_step // 2)]) 

    if debug:
        print(input_vectors_final.shape)
        print(output_vectors_final.shape)
        print(text_vectors_final.shape)
        print(gest_exist_vectors_final.shape)

    return input_vectors_final, text_vectors_final, gest_exist_vectors_final, output_vectors_final

    @staticmethod
    def _get_frankmocap_upper_body_idxs():
        """
        Return the indices corresponding to the XYZ axes of all upper body joints in the motion dataset.
        """
        # TODO: add FRANKMOCAP_JOINT_NAMES to the tools
        joint_names = tools.FRANKMOCAP_JOINT_NAMES
        lower_body_joints = ["hip", "knee", "ankle", "toe"]
        is_lower_body = lambda name: any([x in name.lower() for x in lower_body_joints])

        # Ignore lower body joints
        joint_idxs = [
            i for i, name in enumerate(joint_names) if not is_lower_body(name)
        ]

        # Each joint has 3-dimensional rotations, so we retrieve the index for each
        axis_idxs = np.concatenate(
            [np.array([3 * i, 3 * i + 1, 3 * i + 2]) for i in joint_idxs]
        )

        return axis_idxs


def create_dataset(dataset_name, embedding_model, args, save_in_separate_files):
    """
    Create a dataset using the "encode_vectors" function, 
    then save the input features and the labels as .npy files.

    Args:
        dataset_name:           dataset name ('train', 'test' or 'dev')
        embedding_model:        the embedding model to encode the text with
        save_in_separate_files: if True, the datapoints will be saved in separate files instead of a single
                                numpy array (intended use is with the test/dev dataset!) 
        args:                   see 'data_params.py' for details
    """
    csv_path = path.join(args.proc_data_dir, f"{dataset_name}-dataset-info.csv")
    data_csv = pd.read_csv(csv_path)
    if save_in_separate_files:
        save_dir = path.join(args.proc_data_dir, f'{dataset_name}_inputs') # e.g. dataset/processed/dev_inputs/
        
        if not path.isdir(save_dir):
            os.makedirs(save_dir)

        _save_data_as_sequences(data_csv, save_dir, embedding_model, dataset_name, args)
    else:
        save_dir = args.proc_data_dir

        _save_dataset(data_csv, save_dir, embedding_model, dataset_name, args)

def _save_data_as_sequences(data_csv, save_dir, embedding_model, dataset_name, args):
    """Save the datapoints in 'data_csv' as separate files to 'save_dir'.""" 
    # TODO: add "gest_exist_dir" argument to the args...

    for i in tqdm.trange(len(data_csv)):
        text_file = data_csv['wav_filename'][i][:-3] + "json"
        audio_file = data_csv['wav_filename'][i]
        # Assuming there will be no filename with 3 digits in the name

        gesture_file_number = audio_file[-6: -4:] if audio_file[-6].isnumeric() else audio_file[-5]
        input_vectors, text_vectors, gest_exist_vectors, _ = _encode_vectors(audio_file,
                                                         data_csv['bvh_filename'][i],
                                                         text_file, 
                                                         path.join(args.gest_exist_dir, f"V{int(gesture_file_number)}.npy"),
                                                         embedding_model, mode=dataset_name, 
                                                         args=args, augment_with_context=False)

        filename    = data_csv['wav_filename'][i].split("/")[-1]
        filename    = filename.split(".")[0] # strip the extension from the filename
        
        x_save_path = path.join(save_dir, f'X_{dataset_name}_{filename}.npy')
        t_save_path = path.join(save_dir, f'T_{dataset_name}_{filename}.npy')
        z_save_path = path.join(save_dir, f'Z_{dataset_name}_{filename}.npy')

        np.save(x_save_path, input_vectors)
        np.save(t_save_path, text_vectors)
        np.save(z_save_path, gest_exist_vectors)

def _save_dataset(data_csv, save_dir, embedding_model, dataset_name, args):
    """Save the datapoints in 'data_csv' into three (speech, transcript, label) numpy arrays in 'save_dir'."""
    for i in tqdm.trange(len(data_csv)):
        text_file = data_csv['wav_filename'][i][:-3] + "json"
        audio_file = data_csv['wav_filename'][i]

        gesture_file_number = audio_file[-6: -4:] if audio_file[-6].isnumeric() else audio_file[-5]
        
        input_vectors, text_vectors, gest_exist_vectors, output_vectors = _encode_vectors(audio_file,
                                                                     data_csv['bvh_filename'][i],
                                                                     text_file, 
                                                                     path.join(args.gest_exist_dir, f"V{int(gesture_file_number)}.npy"),
                                                                     embedding_model, mode=dataset_name,
                                                                     args=args, augment_with_context=True)
        if i == 0:
            X = input_vectors
            T = text_vectors
            Y = output_vectors
            Z = gest_exist_vectors

        else:
            X = np.concatenate((X, input_vectors),  axis=0)
            Y = np.concatenate((Y, output_vectors), axis=0)
            T = np.concatenate((T, text_vectors),   axis=0)
            Z = np.concatenate((Z, gest_exist_vectors), axis=0)
    
    x_save_path = path.join(save_dir, f"X_{dataset_name}.npy")
    t_save_path = path.join(save_dir, f"T_{dataset_name}.npy")
    y_save_path = path.join(save_dir, f"Y_{dataset_name}.npy")
    z_save_path = path.join(save_dir, f"Z_{dataset_name}.npy")

    np.save(x_save_path, X)
    np.save(t_save_path, T)
    np.save(y_save_path, Y)
    np.save(z_save_path, Z)

    print(f"Final dataset sizes:\n  X: {X.shape}\n  T: {T.shape}\n  Y: {Y.shape}\n Z: {Z.shape}")

def create_embedding(name):
    if name == "BERT":
        tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
        bert_model = BertModel.from_pretrained('bert-base-cased')

        return tokenizer, bert_model
    elif name == "FastText":
        return FastText()
    else:
        print(f"ERROR: Unknown embedding type '{args.text_embedding}'! Supported embeddings: 'BERT' and 'FastText'.")
        exit(-1)
        
if __name__ == "__main__":
    args = processing_argparser.parse_args()
  
    # Check if the dataset exists
    if not path.exists(args.proc_data_dir):
        abs_path = path.abspath(args.proc_data_dir)

        print(f"ERROR: The given dataset folder for the processed data ({abs_path}) does not exist!")
        print("Please provide the correct folder to the dataset in the '-proc_data_dir' argument.")
        exit(-1)

    embedding_model = create_embedding(args.text_embedding)
    print("Creating datasets...")
    print("Creating train dataset...")
    create_dataset('train', embedding_model, args, save_in_separate_files=False)
    print("Creating dev dataset...")
    create_dataset('dev',   embedding_model, args, save_in_separate_files=False)

    print("Creating test sequences")
    create_dataset('dev',  embedding_model, args, save_in_separate_files=True)
    create_dataset('test', embedding_model, args, save_in_separate_files=True)

    abs_path = path.abspath(args.proc_data_dir)
    print(f"Datasets are created and saved at {abs_path} !")
