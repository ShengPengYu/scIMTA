import math
import tensorflow.keras.backend as K
from tensorflow.keras.models import Model
from tensorflow.keras.losses import MSE, KLD
from tensorflow.keras.layers import Dense, Dropout, Input, Lambda
from spektral.layers import GraphAttention, GraphConvSkip, TAGConv
from scIMTA.losses import dist_loss
from tensorflow.keras.initializers import GlorotUniform
from scIMTA.layers import *
import tensorflow_probability as tfp
import numpy as np
from sklearn import metrics

class scIMTA(tf.keras.Model):

    def __init__(self, X, adj, adj_n, hidden_dim=128, latent_dim=15, dec_dim=[128, 256, 512], adj_dim=32):
        super(scIMTA, self).__init__()
        self.latent_dim = latent_dim
        self.X = X
        self.adj = np.float32(adj)
        self.adj_n = np.float32(adj_n)
        self.n_sample = X.shape[0]
        self.in_dim = X.shape[1]
        self.sparse = False
        initializer = GlorotUniform(seed=7)

        # Encoder
        X_input = Input(shape=self.in_dim)
        h = Dropout(0.2)(X_input)
        A_in = Input(shape=self.n_sample)
        h = GraphConvSkip(channels=hidden_dim, kernel_initializer=initializer, activation="relu")([h, A_in])
        z_mean = GraphConvSkip(channels=latent_dim, kernel_initializer=initializer)([h, A_in])

        self.encoder = Model(inputs=[X_input, A_in], outputs=z_mean, name="encoder")
        clustering_layer = ClusteringLayer(name='clustering')(z_mean)
        self.cluster_model = Model(inputs=[X_input, A_in], outputs=clustering_layer, name="cluster_encoder")

        # Adjacency matrix decoder
        
        dec_in = Input(shape=latent_dim)
        h = Dense(units=adj_dim, activation=None)(dec_in)
        h = Bilinear()(h)
        dec_out = Lambda(lambda z: tf.nn.sigmoid(z))(h) 
        self.decoderA = Model(inputs=dec_in, outputs=dec_out, name="decoder1")
        
        # Expression matrix decoder

        decx_in = Input(shape=latent_dim)
        for i in range(len(dec_dim)):
            if i == 0:
                h = Dense(units=dec_dim[i], activation="relu")(decx_in)
            else:
                h = Dense(units=dec_dim[i], activation="relu")(h)

        P = Dense(units=self.in_dim, activation=tf.nn.softmax, kernel_initializer='glorot_uniform', name='pi')(h)

        self.decoderX = Model(inputs=decx_in, outputs=P, name="decoderX")

    def pre_train(self, epochs=1000, info_step=10, lr=1e-4, W_a=0.3, W_x=1):
      
        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)
        if self.sparse == True:
            self.adj_n = tfp.math.dense_to_sparse(self.adj_n)

        # Training
        for epoch in range(1, epochs + 1):
            with tf.GradientTape(persistent=True) as tape:
                z = self.encoder([self.X, self.adj_n])
                P = self.decoderX(z)
                A_out = self.decoderA(z)
                A_rec_loss = tf.reduce_mean(MSE(self.adj, A_out))
                pre_loss = tf.reduce_mean(-self.X * tf.math.log(tf.clip_by_value(P, 1e-12, 1.0)))
                loss = W_a * A_rec_loss + W_x * pre_loss

            vars = self.trainable_weights
            grads = tape.gradient(loss, vars)
            optimizer.apply_gradients(zip(grads, vars))
            if epoch % info_step == 0:
                print("Epoch", epoch, " Mult_loss:", pre_loss.numpy(), "  A_rec_loss:", A_rec_loss.numpy())

        print("Pre_train Finish!")

    def train(self, epochs=300, lr=5e-4, W_a=0.3, W_x=1, W_c=1.5, info_step=10, n_update=10, centers=None):

        self.cluster_model.get_layer(name='clustering').clusters = centers

        # Training
        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        for epoch in range(0, epochs):

            if epoch % n_update == 0:
                q = self.cluster_model([self.X, self.adj_n])
                p = self.target_distribution(q)

            with tf.GradientTape(persistent=True) as tape:

                z = self.encoder([self.X, self.adj_n])
                q_out = self.cluster_model([self.X, self.adj_n])
                P = self.decoderX(z)
                A_out = self.decoderA(z)
                A_rec_loss = tf.reduce_mean(MSE(self.adj, A_out))
                pre_loss = tf.reduce_mean(-self.X * tf.math.log(tf.clip_by_value(P, 1e-12, 1.0)))
                cluster_loss = tf.reduce_mean(KLD(q_out, p))
                tot_loss = W_a * A_rec_loss + W_x * pre_loss + W_c * cluster_loss

            vars = self.trainable_weights
            grads = tape.gradient(tot_loss, vars)
            optimizer.apply_gradients(zip(grads, vars))

            if epoch % info_step == 0:
                print("Epoch", epoch, " Mult_loss: ", pre_loss.numpy(), " A_rec_loss: ", A_rec_loss.numpy(),
                      " cluster_loss: ", cluster_loss.numpy())

        tf.compat.v1.disable_eager_execution()
        q = tf.constant(q)
        session = tf.compat.v1.Session()
        q = session.run(q)
        self.y_pred = q.argmax(1)
        self.A_out = np.array(A_out)
        self.latent = np.array(z)
        return self

    def target_distribution(self, q):
        q = q.numpy()
        weight = q ** 2 / q.sum(0)
        return (weight.T / weight.sum(1)).T

    def embedding(self, count, adj_n):
        if self.sparse:
            adj_n = tfp.math.dense_to_sparse(adj_n)
        return np.array(self.encoder([count, adj_n]))


class scIMTAL(tf.keras.Model):

    def __init__(self, n_sample, in_dim, batch, hidden_dim=128, latent_dim=15, dec_dim=[128, 256, 512], adj_dim=32):
        super(scIMTAL, self).__init__()
        self.latent_dim = latent_dim
        self.n_sample = n_sample
        self.in_dim = in_dim
        self.batch = batch
        self.num_batch = int(math.ceil(1.0*n_sample/self.batch))
        initializer = GlorotUniform(seed=7)

        # Encoder
        X_input = Input(shape=self.in_dim)
        h = Dropout(0.2)(X_input)
        A_in = Input(shape=self.n_sample)
        h = GraphConvSkip(channels=hidden_dim, kernel_initializer=initializer, activation="relu")([h, A_in])
        z_mean = GraphConvSkip(channels=latent_dim, kernel_initializer=initializer)([h, A_in])

        self.encoder = Model(inputs=[X_input, A_in], outputs=z_mean, name="encoder")
        clustering_layer = ClusteringLayer(name='clustering')(z_mean)
        self.cluster_model = Model(inputs=[X_input, A_in], outputs=clustering_layer, name="cluster_encoder")

        # Adjacency matrix decoder
        
        dec_in = Input(shape=latent_dim)
        h = Dense(units=adj_dim, activation=None)(dec_in)
        h = Bilinear()(h)
        dec_out = Lambda(lambda z: tf.nn.sigmoid(z))(h) 
        self.decoderA = Model(inputs=dec_in, outputs=dec_out, name="decoder1")

        # Expression matrix decoder

        decx_in = Input(shape=latent_dim)
        for i in range(len(dec_dim)):
            if i == 0:
                h = Dense(units=dec_dim[i], activation="relu")(decx_in)
            else:
                h = Dense(units=dec_dim[i], activation="relu")(h)
        P = Dense(units=self.in_dim, activation=tf.nn.softmax, kernel_initializer='glorot_uniform', name='pi')(h)

        self.decoderX = Model(inputs=decx_in, outputs=P, name="decoderX")

    def pre_train(self, x, adj, adj_n, epochs=1000, lr=1e-4, W_a=0.3, W_x=1):
      
        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        # Training
        for epoch in range(1, epochs + 1):
            for batch_idx in range(self.num_batch):
                with tf.GradientTape(persistent=True) as tape:
                    xbatch = x[batch_idx*self.batch : min((batch_idx+1)*self.batch, x.shape[0])]
                    adjo=adj[batch_idx]
                    adjn=tfp.math.dense_to_sparse(adj_n[batch_idx])
                    z = self.encoder([xbatch, adjn])
                    P = self.decoderX(z)
                    A_out = self.decoderA(z)
                    A_rec_loss = tf.reduce_mean(MSE(adjo, A_out))
                    pre_loss = tf.reduce_mean(-xbatch * tf.math.log(tf.clip_by_value(P, 1e-12, 1.0)))
                    loss = W_a * A_rec_loss + W_x * pre_loss

                vars = self.trainable_weights
                grads = tape.gradient(loss, vars)
                optimizer.apply_gradients(zip(grads, vars))
                print('Pretrain epoch [{}/{}], Mult loss:{:.4f}, A_rec_loss:{:.4f}'.format(batch_idx+1, epoch+1, pre_loss.numpy(), A_rec_loss.numpy()))
            
        print("Pre_train Finish!")

    def train(self, x, adj, adj_n, epochs=300, lr=5e-4, W_a=0.3, W_x=1, W_c=1.5, n_update=8, centers=None,tol=1e-3):

        self.centers = centers

        # Training
        optimizer = tf.keras.optimizers.Adam(learning_rate=lr)

        for epoch in range(0, epochs): 

            if epoch % n_update == 0:
                latent = self.embeddingBatch(x, adj_n)
                q = self.soft_assign(latent)
                p = self.target_distribution(q)
            
            for batch_idx in range(self.num_batch):
                with tf.GradientTape(persistent=True) as tape:
                    
                    xbatch = x[batch_idx*self.batch : min((batch_idx+1)*self.batch, x.shape[0])]
                    adjo=adj[batch_idx]
                    adjn=tfp.math.dense_to_sparse(adj_n[batch_idx])
                    pbatch = p[batch_idx*self.batch : min((batch_idx+1)*self.batch, x.shape[0])]

                    z = self.encoder([xbatch , adjn])
                    q_out = self.soft_assign(z)
                    P = self.decoderX(z)
                    A_out = self.decoderA(z)

                    A_rec_loss = tf.reduce_mean(MSE(adjo, A_out))
                    pre_loss = tf.reduce_mean(-xbatch * tf.math.log(tf.clip_by_value(P, 1e-12, 1.0)))
                    cluster_loss = tf.reduce_mean(KLD(q_out, pbatch))
                    tot_loss = W_a * A_rec_loss + W_x * pre_loss + W_c * cluster_loss

                vars = self.trainable_weights
                grads = tape.gradient(tot_loss, vars)
                optimizer.apply_gradients(zip(grads, vars))
                print('Train epoch [{}/{}], Mult loss:{:.4f}, A_rec_loss:{:.4f}, Clu_loss:{:.4f}'
                .format(batch_idx+1, epoch+1, pre_loss.numpy(), A_rec_loss.numpy(), cluster_loss.numpy()))

        tf.compat.v1.disable_eager_execution()
        q = tf.constant(q)
        session = tf.compat.v1.Session()
        q = session.run(q)
        self.y_pred = q.argmax(1)
        self.latent = np.array(latent)
        return self

    def target_distribution(self, q):
        q = q.numpy()
        weight = q ** 2 / q.sum(0)
        return (weight.T / weight.sum(1)).T

    def embedding(self, count, adj_n):
        if self.sparse:
            adj_n = tfp.math.dense_to_sparse(adj_n)
        return np.array(self.encoder([count, adj_n]))

    def embeddingBatch(self, x, adj_n):
        latent = []
        for batch_idx in range(self.num_batch):
            xbatch = tfp.math.dense_to_sparse(x[batch_idx*self.batch : min((batch_idx+1)*self.batch, x.shape[0])])
            adjn=tfp.math.dense_to_sparse(adj_n[batch_idx])
            z = self.encoder([xbatch, adjn])
            latent.append(z)
            # X_out = self.decoderX(z)
        latent = tf.concat(latent,0)
        return np.array(latent)

    def soft_assign(self, latent):
        self.alpha = 1.0
        q = 1.0 / (1.0 + (K.sum(K.square(K.expand_dims(latent, axis=1) - self.centers), axis=2) / self.alpha))
        q **= (self.alpha + 1.0) / 2.0
        q = K.transpose(K.transpose(q) / K.sum(q, axis=1))
        return q
