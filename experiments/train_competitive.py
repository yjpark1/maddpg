import argparse
import numpy as np
import tensorflow as tf
import time
import pickle

import maddpg.common.tf_util as U
from maddpg.trainer.maddpg import MADDPGAgentTrainer
from maddpg.trainer.maddpg import MADDPGAgentTrainerSplit
import tensorflow.contrib.layers as layers

from experiments.scenarios import make_env


def parse_args():
    parser = argparse.ArgumentParser("Reinforcement Learning experiments for multiagent environments")
    # Environment
    parser.add_argument("--scenario", type=str, default="simple_adversary", help="name of the scenario script")
    parser.add_argument("--max-episode-len", type=int, default=25, help="maximum episode length")
    parser.add_argument("--num-episodes", type=int, default=40000, help="number of episodes")
    parser.add_argument("--num-adversaries", type=int, default=0, help="number of adversaries")
    parser.add_argument("--good-policy", type=str, default="maddpg", help="policy for good agents")
    parser.add_argument("--adv-policy", type=str, default="maddpg", help="policy of adversaries")
    # Core training parameters
    parser.add_argument("--lr", type=float, default=1e-2, help="learning rate for Adam optimizer")
    parser.add_argument("--gamma", type=float, default=0.95, help="discount factor")
    parser.add_argument("--batch-size", type=int, default=1024, help="number of episodes to optimize at the same time")
    parser.add_argument("--num-units", type=int, default=64, help="number of units in the mlp")
    # Checkpointing
    parser.add_argument("--exp-name", type=str, default=None, help="name of the experiment")
    parser.add_argument("--save-dir", type=str, default="/tmp/policy/", help="directory in which training state and model should be saved")
    parser.add_argument("--save-rate", type=int, default=1000, help="save model once every time this many episodes are completed")
    parser.add_argument("--load-dir", type=str, default="", help="directory in which training state and model are loaded")
    # Evaluation
    parser.add_argument("--restore", action="store_true", default=False)
    parser.add_argument("--display", action="store_true", default=False)
    parser.add_argument("--benchmark", action="store_true", default=False)
    parser.add_argument("--benchmark-iters", type=int, default=100000, help="number of iterations run for benchmarking")
    parser.add_argument("--benchmark-dir", type=str, default="./benchmark_files/", help="directory where benchmark data is saved")
    parser.add_argument("--plots-dir", type=str, default="./learning_curves/", help="directory where plot data is saved")
    return parser.parse_args()


def mlp_model(input, num_outputs, scope, reuse=False, num_units=64, rnn_cell=None):
    # This model takes as input an observation and returns values of all actions
    with tf.variable_scope(scope, reuse=reuse):
        out = input
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_units, activation_fn=tf.nn.relu)
        out = layers.fully_connected(out, num_outputs=num_outputs, activation_fn=None)
        return out

'''
def get_trainers(env, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainerSplit
    agent = env.agents

    for i in range(env.n):
        if agent[i].adversary:
            team = 'adv'
            trainers.append(trainer(
                "agent_%d" % i, model, agent, team, obs_shape_n, env.action_space, i, arglist,
                local_q_func=(arglist.adv_policy=='ddpg')))
        else:
            team = 'own'
            trainers.append(trainer(
                "agent_%d" % i, model, agent, team, obs_shape_n, env.action_space, i, arglist,
                local_q_func=(arglist.adv_policy == 'ddpg')))

    return trainers
'''
def get_trainers(env, num_adversaries, obs_shape_n, arglist):
    trainers = []
    model = mlp_model
    trainer = MADDPGAgentTrainer
    for i in range(num_adversaries):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.adv_policy=='ddpg')))
    for i in range(num_adversaries, env.n):
        trainers.append(trainer(
            "agent_%d" % i, model, obs_shape_n, env.action_space, i, arglist,
            local_q_func=(arglist.good_policy=='ddpg')))
    return trainers


def split_own_adv(env, z):
    z_own = [y for x, y in zip(env.agents, z) if not x.adversary]
    z_adv = [y for x, y in zip(env.agents, z) if x.adversary]
    return z_own, z_adv


def train(scenario_name, cnt, arglist):
    with U.single_threaded_session():
        # Create environment
        env = make_env(scenario_name)
        seed = cnt + 12345678
        env.seed(seed)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))

        # Initialize
        U.initialize()

        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        episode_rewards_own = [0.0]  # sum of rewards for our agents
        episode_rewards_adv = [0.0]  # sum of rewards for adversary agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards_own = []  # sum of rewards for training curve
        final_ep_rewards_adv = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        obs_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        print('Starting iterations...')
        while True:
            # get action
            action_n = [agent.action(obs) for agent, obs in zip(trainers, obs_n)]
            # environment step
            new_obs_n, rew_n, done_n, info_n = env.step(action_n)

            # make shared reward
            rew_own, rew_adv = split_own_adv(env, rew_n)
            rew_own = np.sum(rew_own)
            rew_adv = np.sum(rew_adv)
            rew_shared = []
            for a in env.agents:
                if a.adversary:
                    rew_shared.append(rew_adv)
                else:
                    rew_shared.append(rew_own)

            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            # collect experience
            for i, agent in enumerate(trainers):
                agent.experience(obs_n[i], action_n[i], rew_shared[i], new_obs_n[i], done_n[i], terminal)
            obs_n = new_obs_n

            for i, (ag, rew) in enumerate(zip(env.agents, rew_n)):
                if ag.adversary:
                    episode_rewards_adv[-1] += rew
                else:
                    episode_rewards_own[-1] += rew
                agent_rewards[i][-1] += rew

            if done or terminal:
                obs_n = env.reset()
                episode_step = 0
                episode_rewards_own.append(0)
                episode_rewards_adv.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                for i, info in enumerate(info_n):
                    agent_info[-1][i].append(info_n['n'])
                if train_step > arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                    break
                continue

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # update all trainers, if not in display or benchmark mode
            loss = None
            for agent in trainers:
                agent.preupdate()

            for agent in trainers:
                agent.update(trainers, train_step)

            '''
            trainers_own = [x for a, x in zip(env.agents, trainers) if a.adversary]
            trainers_adv = [x for a, x in zip(env.agents, trainers) if not a.adversary]
            for env_agent, agent in zip(env.agents, trainers):
                if env_agent.adversary:
                    agent.update(trainers_adv, train_step)
                else:
                    agent.update(trainers_own, train_step)
            '''
            # save model, display training output
            if terminal and (len(episode_rewards_own) % arglist.save_rate == 0):
                # print statement depends on whether or not there are adversaries
                print("steps: {}, episodes: {}, reward (our): {}, reward (adv): {}, time: {}".format(
                    train_step, len(episode_rewards_own),
                    round(np.mean(episode_rewards_own[-arglist.save_rate:]), 3),
                    round(np.mean(episode_rewards_adv[-arglist.save_rate:]), 3),
                    round(time.time() - t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards_own.append(np.mean(episode_rewards_own[-arglist.save_rate:]))
                final_ep_rewards_adv.append(np.mean(episode_rewards_adv[-arglist.save_rate:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-arglist.save_rate:]))

            # saves final episode reward for plotting training curve later
            if len(episode_rewards_own) > arglist.num_episodes:
                hist = {'reward_episodes_own': episode_rewards_own,
                        'reward_episodes_adv': episode_rewards_adv,
                        'reward_episodes_by_agents': agent_rewards}
                file_name = 'Models/history_' + scenario_name + '_' + str(cnt) + '.pkl'
                with open(file_name, 'wb') as fp:
                    pickle.dump(hist, fp)
                print('...Finished total of {} episodes.'.format(len(episode_rewards_own)))
                file_model = 'Models/' + scenario_name + '_fin_' + str(cnt) + '.pkl'
                U.save_state(file_model, saver=saver)
                break

def test(scenario_name, cnt, arglist):
    with U.single_threaded_session():
        # Create environment
        env = make_env(scenario_name, local_observation=False)
        seed = cnt + 12345678
        env.seed(seed)
        # Create agent trainers
        obs_shape_n = [env.observation_space[i].shape for i in range(env.n)]
        num_adversaries = min(env.n, arglist.num_adversaries)
        trainers = get_trainers(env, num_adversaries, obs_shape_n, arglist)
        print('Using good policy {} and adv policy {}'.format(arglist.good_policy, arglist.adv_policy))

        # Initialize
        U.initialize()

        # Load previous results, if necessary
        if arglist.load_dir == "":
            arglist.load_dir = arglist.save_dir
        if arglist.display or arglist.restore or arglist.benchmark:
            print('Loading previous state...')
            U.load_state(arglist.load_dir)

        episode_rewards = [0.0]  # sum of rewards for all agents
        agent_rewards = [[0.0] for _ in range(env.n)]  # individual agent reward
        final_ep_rewards = []  # sum of rewards for training curve
        final_ep_ag_rewards = []  # agent rewards for training curve
        agent_info = [[[]]]  # placeholder for benchmarking info
        saver = tf.train.Saver()
        file_model = 'D:\MEGA/GitHub/multiagent_rl/Models/env_origin/maddpg/' + scenario_name + '_fin_' + str(cnt) + '.pkl'
        U.load_state(file_model, saver=saver)

        obs_n = env.reset()
        episode_step = 0
        train_step = 0
        t_start = time.time()

        print('Starting iterations...')
        while True:
            # get action
            action_n = [agent.action(obs) for agent, obs in zip(trainers,obs_n)]
            # environment step
            new_obs_n, rew_n, done_n, info_n = env.step(action_n)

            # make shared reward
            rew_shared = [np.sum(rew_n)] * env.n

            episode_step += 1
            done = all(done_n)
            terminal = (episode_step >= arglist.max_episode_len)
            # collect experience
            for i, agent in enumerate(trainers):
                agent.experience(obs_n[i], action_n[i], rew_shared[i], new_obs_n[i], done_n[i], terminal)
            obs_n = new_obs_n

            for i, rew in enumerate(rew_n):
                episode_rewards[-1] += rew
                agent_rewards[i][-1] += rew

            if done or terminal:
                obs_n = env.reset()
                episode_step = 0
                episode_rewards.append(0)
                for a in agent_rewards:
                    a.append(0)
                agent_info.append([[]])

            # increment global step counter
            train_step += 1

            # for benchmarking learned policies
            if arglist.benchmark:
                for i, info in enumerate(info_n):
                    agent_info[-1][i].append(info_n['n'])
                if train_step > arglist.benchmark_iters and (done or terminal):
                    file_name = arglist.benchmark_dir + arglist.exp_name + '.pkl'
                    print('Finished benchmarking, now saving...')
                    with open(file_name, 'wb') as fp:
                        pickle.dump(agent_info[:-1], fp)
                    break
                continue

            # for displaying learned policies
            if arglist.display:
                time.sleep(0.1)
                env.render()
                continue

            # save model, display training output
            if terminal and (len(episode_rewards) % 10 == 0):
                # print statement depends on whether or not there are adversaries
                if num_adversaries == 0:
                    print("steps: {}, episodes: {}, mean episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-10:]), round(time.time()-t_start, 3)))
                else:
                    print("steps: {}, episodes: {}, mean episode reward: {}, agent episode reward: {}, time: {}".format(
                        train_step, len(episode_rewards), np.mean(episode_rewards[-10:]),
                        [np.mean(rew[-10:]) for rew in agent_rewards], round(time.time()-t_start, 3)))
                t_start = time.time()
                # Keep track of final episode reward
                final_ep_rewards.append(np.mean(episode_rewards[-10:]))
                for rew in agent_rewards:
                    final_ep_ag_rewards.append(np.mean(rew[-10:]))

            # saves final episode reward for plotting training curve later
            if len(episode_rewards) > arglist.num_episodes:
                hist = {'reward_episodes': episode_rewards,
                        'reward_episodes_by_agents': agent_rewards}
                file_name = 'Models/test_history_' + scenario_name + '_' + str(cnt) + '.pkl'
                with open(file_name, 'wb') as fp:
                    pickle.dump(hist, fp)
                print('...Finished total of {} episodes.'.format(len(episode_rewards)))
                break

if __name__ == '__main__':
    import os
    os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"  # see issue #152
    os.environ["CUDA_VISIBLE_DEVICES"] = '0'

    TEST_ONLY = False

    scenarios = ['simple_adversary', 'simple_crypto', 'simple_push',
                 'simple_tag', 'simple_world_comm']

    for scenario_name in scenarios:
        for cnt in range(10):
            tf.reset_default_graph()
            seed = cnt + 12345678
            np.random.seed(seed)
            tf.random.set_random_seed(seed)
            arglist = parse_args()
            if TEST_ONLY:
                test(scenario_name, cnt, arglist)
            else:
                train(scenario_name, cnt, arglist)
