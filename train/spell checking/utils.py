import re
import pandas as pd
import os
import unidecode
import numpy as np
import nltk

from tensorflow.keras.models import Model, load_model
from tensorflow.keras.layers import Input

from model import truncated_acc, truncated_loss

from sklearn.model_selection import train_test_split

np.random.seed(2434)

SOS = '\t' # start of sequence.
EOS = '*' # end of sequence.
CHARS = list('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ')
REMOVE_CHARS = '[#$%"\+@<=>!&,-.?:;()*\[\]^_`{|}~/\d\t\n\r\x0b\x0c]'


class CharacterTable(object):
    def __init__(self, chars):
        self.chars = sorted(set(chars))
        self.char2index = dict((c, i) for i, c in enumerate(self.chars))
        self.index2char = dict((i, c) for i, c in enumerate(self.chars))
        self.size = len(self.chars)
    
    def encode(self, C, nb_rows):
        x = np.zeros((nb_rows, len(self.chars)), dtype=np.float32)
        for i, c in enumerate(C):
            x[i, self.char2index[c]] = 1.0
        return x

    def decode(self, x, calc_argmax=True):
        if calc_argmax:
            indices = x.argmax(axis=-1)
        else:
            indices = x
        chars = ''.join(self.index2char[ind] for ind in indices)
        return indices, chars

    def sample_multinomial(self, preds, temperature=1.0):
        # Reshaped to 1D array of shape (nb_chars,).
        preds = np.reshape(preds, len(self.chars)).astype(np.float64)
        preds = np.log(preds) / temperature
        exp_preds = np.exp(preds)
        preds = exp_preds / np.sum(exp_preds)
        probs = np.random.multinomial(1, preds, 1)
        index = np.argmax(probs)
        char  = self.index2char[index]
        return index, char

def preprocess(data_path, input, output):
    input_path = os.path.join(data_path, input)
    output_path = os.path.join(data_path, output)
    with open(input_path, "r") as raw_data, \
            open(output_path, "w") as out:
        for line in raw_data:
            parts = line.split(" +++$+++ ")
            dialog_line = parts[-1]
            s = dialog_line.strip()
            out.write(s + "\n")

def split_text(data_path, input_path):
    file_path = os.path.join(data_path, input_path)
    with open(file_path, "r") as f:
        data = f.read().split('\n')
        data = np.array(data)  #convert array to numpy type array
        train, test = train_test_split(data,test_size=0.2)
        train_text = '\n'.join([i for i in train[1:]])
        test_text = '\n'.join([i for i in test[1:]])
        return train_text, test_text

def read_text(data_path, book):
    text = ''
    file_path = os.path.join(data_path, book)
    strings = unidecode.unidecode(open(file_path).read())
    text += strings + ' '
    return text

def tokenize(text):
    tokens = [re.sub(REMOVE_CHARS, '', token)
              for token in re.split("[-\n ]", text)]
    return tokens

def add_spelling_errors(token, error_rate):
    assert(0.0 <= error_rate < 1.0)
    if len(token) < 3:
        return token
    rand = np.random.rand()
    prob = error_rate / 4.0
    if rand < prob:
        random_char_index = np.random.randint(len(token))
        token = token[:random_char_index] + np.random.choice(CHARS) \
                + token[random_char_index + 1:]
    elif prob * 2 < rand < prob * 3:
        random_char_index = np.random.randint(len(token))
        token = token[:random_char_index] + np.random.choice(CHARS) \
                + token[random_char_index:]
    elif prob < rand < prob * 2:
        random_char_index = np.random.randint(len(token))
        token = token[:random_char_index] + token[random_char_index + 1:]
    elif prob * 3 < rand < prob * 4:
        random_char_index = np.random.randint(len(token) - 1)
        token = token[:random_char_index]  + token[random_char_index + 1] \
                + token[random_char_index] + token[random_char_index + 2:]
    else:
        pass
    return token


def transform(tokens, maxlen, error_rate=0.3, shuffle=True):
    if shuffle:
        print('Shuffling data.')
        np.random.shuffle(tokens)
    encoder_tokens = []
    decoder_tokens = []
    target_tokens = []
    for token in tokens:
        encoder = add_spelling_errors(token, error_rate=error_rate)
        encoder += EOS * (maxlen - len(encoder))
        encoder_tokens.append(encoder)
    
        decoder = SOS + token
        decoder += EOS * (maxlen - len(decoder))
        decoder_tokens.append(decoder)
    
        target = decoder[1:]
        target += EOS * (maxlen - len(target))
        target_tokens.append(target)
        
        assert(len(encoder) == len(decoder) == len(target))
    return encoder_tokens, decoder_tokens, target_tokens


def batch(tokens, maxlen, ctable, batch_size=128, reverse=False):
    def generate(tokens, reverse):
        while(True):
            for token in tokens:
                if reverse:
                    token = token[::-1]
                yield token
    
    token_iterator = generate(tokens, reverse)
    data_batch = np.zeros((batch_size, maxlen, ctable.size),
                          dtype=np.float32)
    while(True):
        for i in range(batch_size):
            token = next(token_iterator)
            data_batch[i] = ctable.encode(token, maxlen)
        yield data_batch


def datagen(encoder_iter, decoder_iter, target_iter):
    inputs = zip(encoder_iter, decoder_iter)
    while(True):
        encoder_input, decoder_input = next(inputs)
        target = next(target_iter)
        yield ([encoder_input, decoder_input], target)


def decode_sequences(inputs, targets, input_ctable, target_ctable,
                     maxlen, reverse, encoder_model, decoder_model,
                     nb_examples, sample_mode='argmax', random=True):
    input_tokens = []
    target_tokens = []
    
    if random:
        indices = np.random.randint(0, len(inputs), nb_examples)
    else:
        indices = range(nb_examples)
        
    for index in indices:
        input_tokens.append(inputs[index])
        target_tokens.append(targets[index])
    input_sequences = batch(input_tokens, maxlen, input_ctable,
                            nb_examples, reverse)
    input_sequences = next(input_sequences)

    states_value = encoder_model.predict(input_sequences)
    
    target_sequences = np.zeros((nb_examples, 1, target_ctable.size))

    target_sequences[:, 0, target_ctable.char2index[SOS]] = 1.0

    decoded_tokens = [''] * nb_examples
    for _ in range(maxlen):
        char_probs, h, c = decoder_model.predict(
            [target_sequences] + states_value)

        target_sequences = np.zeros((nb_examples, 1, target_ctable.size))

        sampled_chars = []
        for i in range(nb_examples):
            if sample_mode == 'argmax':
                next_index, next_char = target_ctable.decode(
                    char_probs[i], calc_argmax=True)
            elif sample_mode == 'multinomial':
                next_index, next_char = target_ctable.sample_multinomial(
                    char_probs[i], temperature=0.5)
            else:
                raise Exception(
                    "`sample_mode` accepts `argmax` or `multinomial`.")
            decoded_tokens[i] += next_char
            sampled_chars.append(next_char) 
            # Update target sequence with index of next character.
            target_sequences[i, 0, next_index] = 1.0

        stop_char = set(sampled_chars)
        if len(stop_char) == 1 and stop_char.pop() == EOS:
            break
            
        # Update states.
        states_value = [h, c]
    
    # Sampling finished.
    input_tokens   = [re.sub('[%s]' % EOS, '', token)
                      for token in input_tokens]
    target_tokens  = [re.sub('[%s]' % EOS, '', token)
                      for token in target_tokens]
    decoded_tokens = [re.sub('[%s]' % EOS, '', token)
                      for token in decoded_tokens]
    return input_tokens, target_tokens, decoded_tokens


def restore_model(path_to_full_model, hidden_size):
    model = load_model(path_to_full_model, custom_objects={
        'truncated_acc': truncated_acc, 'truncated_loss': truncated_loss})
    
    encoder_inputs = model.input[0] # encoder_data
    encoder_lstm1 = model.get_layer('encoder_lstm_1')
    encoder_lstm2 = model.get_layer('encoder_lstm_2')
    
    encoder_outputs = encoder_lstm1(encoder_inputs)
    _, state_h, state_c = encoder_lstm2(encoder_outputs)
    encoder_states = [state_h, state_c]
    encoder_model = Model(inputs=encoder_inputs, outputs=encoder_states)

    decoder_inputs = model.input[1] # decoder_data
    decoder_state_input_h = Input(shape=(hidden_size,))
    decoder_state_input_c = Input(shape=(hidden_size,))
    decoder_states_inputs = [decoder_state_input_h, decoder_state_input_c]
    decoder_lstm = model.get_layer('decoder_lstm')
    decoder_outputs, state_h, state_c = decoder_lstm(
        decoder_inputs, initial_state=decoder_states_inputs)
    decoder_states = [state_h, state_c]
    decoder_softmax = model.get_layer('decoder_softmax')
    decoder_outputs = decoder_softmax(decoder_outputs)
    decoder_model = Model(inputs=[decoder_inputs] + decoder_states_inputs,
                          outputs=[decoder_outputs] + decoder_states)
    return encoder_model, decoder_model
